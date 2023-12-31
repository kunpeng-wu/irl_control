from irl_control.robot import RobotState
import numpy as np
import mujoco_py as mjp
from transforms3d.derivations.quaternions import qmult
from transforms3d.quaternions import qconjugate
from transforms3d.euler import quat2euler, euler2quat
from transforms3d.utils import normalized_vector
from typing import Dict, Tuple, List
from irl_control import Robot, Device
from irl_control.utils import ControllerConfig, Target
from irl_control.device import DeviceState

class OSC():
    """
        OSC provides Operational Space Control for a given Robot.
        This controller accepts targets as a input, and generates a control signal
        for the devices that are linked to the targets.
    """
    def __init__(self, robot: Robot, sim, input_device_configs: List[Tuple[str, Dict]], nullspace_config : Dict = None, use_g=True, admittance=False):
        self.sim = sim
        self.robot = robot
        
        # Create a dict, device_configs, which maps a device name to a
        # ControllerConfig. ControllerConfig is a lightweight wrapper
        # around the dict class to add some desired methods
        self.device_configs = dict()
        for dcnf in input_device_configs:
            self.device_configs[dcnf[0]] = ControllerConfig(dcnf[1])
        self.nullspace_config = nullspace_config
        self.use_g = use_g
        self.admittance = admittance
        
        # Obtain the controller configuration parameters
        # and calculate the task space gains
        for device_name in self.device_configs.keys():
            kv, kp, ko = self.device_configs[device_name].get_params(['kv', 'kp', 'ko'])
            task_space_gains = np.array([kp] * 3 + [ko] * 3)
            self.device_configs[device_name]['task_space_gains'] = task_space_gains
            self.device_configs[device_name]['lamb'] = task_space_gains / kv

    def __Mx(self, J, M):
        """
            Returns the inverse of the task space inertia matrix
            Parameters
            ----------
            J: Jacobian matrix
            M: inertia matrix
        """
        M_inv = self.__svd_solve(M)
        Mx_inv = np.dot(J, np.dot(M_inv, J.T))
        threshold = 1e-4
        if abs(np.linalg.det(Mx_inv)) >= threshold:
            Mx = self.__svd_solve(Mx_inv)
        else:
            Mx = np.linalg.pinv(Mx_inv, rcond=threshold*0.1)
        return Mx, M_inv
    

    def __svd_solve(self, A):
        """
            Use the SVD Method to calculate the inverse of a matrix
            Parameters
            ----------
            A: Matrix
        """
        u, s, v = np.linalg.svd(A)
        Ainv = np.dot(v.transpose(), np.dot(np.diag(s**-1), u.transpose()))
        return Ainv
    
    def __limit_vel(self, u_task: np.ndarray, device: Device):
        """
            Limit the velocity of the task space control vector
            Parameters
            ----------
            u_task: array of length 6 corresponding to the task space control
        """
        if device.max_vel is not None:
            kv, kp, ko, lamb = self.device_configs[device.name].get_params(['kv', 'kp', 'ko', 'lamb'])
            scale = np.ones(6)
            
            # Apply the sat gains to the x,y,z components
            norm_xyz = np.linalg.norm(u_task[:3])
            sat_gain_xyz = device.max_vel[0] / kp * kv
            scale_xyz = device.max_vel[0] / kp * kv
            if norm_xyz > sat_gain_xyz:
                scale[:3] *= scale_xyz / norm_xyz
            
            # Apply the sat gains to the a,b,g components
            norm_abg = np.linalg.norm(u_task[3:])
            sat_gain_abg = device.max_vel[1] / ko * kv
            scale_abg = device.max_vel[1] / ko * kv
            if norm_abg > sat_gain_abg:
                scale[3:] *= scale_abg / norm_abg
            u_task = kv * scale * lamb * u_task
        else:
            print("Device max_vel must be set in the yaml file!")
            raise Exception

        return u_task

    def calc_error(self, target: Target, device: Device):
        """
            Compute the difference between the target and device EE
            for the x,y,z and a,b,g components
        """
        u_task = np.zeros(6)
        # Calculate x,y,z error
        if np.sum(device.ctrlr_dof_xyz) > 0:
            diff = device.get_state(DeviceState.EE_XYZ) - target.get_xyz()
            u_task[:3] = diff
        
        # Calculate a,b,g error
        if np.sum(device.ctrlr_dof_abg) > 0:
            t_rot = target.get_quat()
            q_d = normalized_vector(t_rot)
            q_r = np.array(qmult(q_d, qconjugate(device.get_state(DeviceState.EE_QUAT))))
            u_task[3:] = quat2euler(qconjugate(q_r)) # -q_r[1:] * np.sign(q_r[0])
        return u_task
    
    def generate(self, targets: Dict[str, Target]):
        """
            Generate forces for the corresponding devices which are in the 
            robot's sub-devices. Accepts a dictionary of device names (keys), 
            which map to a Target. 
            Parameters
            ----------
            targets: dict of device names mapping to Target objects
        """
        if self.robot.is_using_sim() is False:
            assert self.robot.is_running(), "Robot must be running!"
        
        robot_state = self.robot.get_all_states()
        # Get the Jacobian for the all of devices passed in
        Js, J_idxs = robot_state[RobotState.J]
        # J, J_idxs = self.robot.get_jacobian(targets.keys())
        J = np.array([])
        for device_name in targets.keys():
            J = np.vstack([J, Js[device_name]]) if J.size else Js[device_name]
        # Get the inertia matrix for the robot
        # M = self.robot.get_M()
        M = robot_state[RobotState.M]
        
        # Compute the inverse matrices used for task space operations 
        Mx, M_inv = self.__Mx(J, M)

        # Initialize the control vectors and sim data needed for control calculations
        # dq = self.robot.get_dq()
        dq = robot_state[RobotState.DQ]
        
        dx = np.dot(J, dq)
        uv_all = np.dot(M, dq)
        u_all = np.zeros(self.robot.num_joints_total)
        u_task_all = np.array([])
        ext_f = np.array([])

        for device_name, target in targets.items():
            device = self.robot.get_device(device_name)
            # Calculate the error from the device EE to target
            u_task = self.calc_error(target, device)
            stiffness = np.array(self.device_configs[device_name]['k'] + [1]*3)
            damping = np.array(self.device_configs[device_name]['d'] + [1]*3)
            # Apply gains to the error terms
            if device.max_vel is not None:
                u_task = self.__limit_vel(u_task, device)
                u_task *= stiffness 
            else:
                task_space_gains = self.device_configs[device.name]['task_space_gains']
                u_task *= task_space_gains * stiffness

            # Apply kv gain
            kv = self.device_configs[device.name]['kv']
            target_vel = np.hstack([target.get_xyz_vel(), target.get_abg_vel()])
            if np.all(target_vel) == 0:
                u_all[device.joint_ids_all] = -1 * kv * uv_all[device.joint_ids_all]
            else:
                diff = dx[J_idxs[device_name]] - np.array(target_vel)[device.ctrlr_dof]
                u_task[device.ctrlr_dof] += kv * diff * damping[device.ctrlr_dof]
            
            force = np.append(robot_state[device_name][DeviceState.FORCE], robot_state[device_name][DeviceState.TORQUE])
            ext_f = np.append(ext_f, force[device.ctrlr_dof])
            u_task_all = np.append(u_task_all, u_task[device.ctrlr_dof])
        
        # Transform task space signal to joint space
        if self.admittance is True:
            u_all -= np.dot(J.T, np.dot(Mx, u_task_all+ext_f))
        else:
            u_all -= np.dot(J.T, np.dot(Mx, u_task_all))
        
        # Apply gravity forces
        if self.use_g:
            u_all += self.sim.data.qfrc_bias[self.robot.joint_ids_all]
        
        # Apply the nullspace controller using the specified parameters
        # (if passed to constructor / initialized)
        if self.nullspace_config is not None:
            damp_kv = self.nullspace_config['kv']
            u_null = np.dot(M, -damp_kv*dq)
            Jbar = np.dot(M_inv, np.dot(J.T, Mx))
            null_filter = np.eye(self.robot.num_joints_total) - np.dot(J.T, Jbar.T)
            u_all += np.dot(null_filter, u_null)

        # Return the forces and indices to apply the forces
        forces = []
        force_idxs = []       
        for dev_name in targets.keys():
            dev = self.robot.sub_devices_dict[dev_name]
            forces.append(u_all[dev.actuator_trnids])
            force_idxs.append(dev.ctrl_idxs)
        
        return force_idxs, forces 