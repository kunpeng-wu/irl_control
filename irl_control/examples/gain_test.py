from mujoco_py import GlfwContext
from mujoco_py.mjviewer import MjViewer
import numpy as np
from typing import Tuple, Dict
import threading
from irl_control import MujocoApp, OSC
from irl_control.utils import Target
from irl_control.device import DeviceState

"""
The purpose of this example is to test out the robot configuration
to see how well the gains perform on stabilizing the base and the
arm that does move rapidly. One of the arms in this demo will move
wildly to test out this stabilization capability.
"""
class GainTest(MujocoApp):
    """
    Implements the OSC and Dual UR5 robot
    """
    def __init__(self, robot_config_file : str =None, scene_file : str = None):
        # Initialize the Parent class with the config file
        super().__init__(robot_config_file, scene_file)
        # Specify the robot in the scene that we'd like to use
        self.robot = self.get_robot(robot_name="DualUR5")
        
        # Specify the controller configuations that should be used for
        # the corresponding devices
        osc_device_configs = [
            ('base', self.get_controller_config('osc0')),
            ('ur5right', self.get_controller_config('osc2')),
            ('ur5left', self.get_controller_config('osc2'))
        ]

        # Get the configuration for the nullspace controller
        nullspace_config = self.get_controller_config('nullspace')
        self.controller = OSC(self.robot, self.sim, osc_device_configs, nullspace_config)

        # Start collecting device states from simulator
        # NOTE: This is necessary when you are using OSC, as it assumes
        #       that the robot.start() thread is running.
        self.robot_data_thread = threading.Thread(target=self.robot.start)
        self.robot_data_thread.start()
        
        # Keep track of device target errors
        self.errors = dict()
        self.errors['ur5right'] = 0
        self.viewer = MjViewer(self.sim)
        self.viewer.cam.azimuth = 90
        self.viewer.cam.elevation = -30
        self.viewer.cam.distance = self.model.stat.extent*1.5
    
    def gen_figure_eight_path(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generates a figure 8 path.
        """
        left_wp = np.array([
            [-0.3, -0.2, 0.2],
            [-0.4, -0.2, 0.3],
            [-0.7, -0.3, 0.3],
            [-0.8, -0.4, 0.5],
            [-0.9, -0.35, 0.7],
            [-0.9, -0.2, 0.5],
            [-0.9, -0.6, 0.2],
            [-0.7, -0.7, 0.2],
            [-0.4, -0.8, 0.3],
        ])

        interp = np.linspace(left_wp[0], left_wp[1], 5)
        for i in range(1, left_wp.shape[0] - 1):
            arr = np.linspace(left_wp[i], left_wp[i+1], 5)
            interp = np.vstack((interp, arr))

        arr = np.linspace(left_wp[left_wp.shape[0] - 1], left_wp[0], 5)
        left_wp = np.vstack((interp, arr))
        right_wp = np.copy(left_wp)
        right_wp[:,[0,1]] *= -1
        
        return (right_wp, left_wp)
    

    def gen_waypoint_path(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generates a back-and-forth path.
        """
        # Generate waypoints for the right arm
        right_wp = np.array([
            [0.8, 0.6, 0.7],
            [0.8, -0.6, 0.7],
        ])
        
        # Generate waypoints for the left arm
        left_wp = np.array([
            [-0.5, -0.5, 0.5],
        ])

        return (right_wp, left_wp)

    def run(self, demo_type: str, demo_duration: int):
        """
        This is the main function that gets called. Uses the 
        Operational Space Controller to control the DualUR5
        and guide the arms and base to specified target waypoints.
        
        Parameters
        ----------
        demo_type: str
            The name of the demo that should be run
        """
        # Choose demo type
        if demo_type == 'gain_test':
            right_wps, left_wps = self.gen_waypoint_path()
        elif demo_type == 'figure8':
            right_wps, left_wps = self.gen_figure_eight_path()
        else:
            print("Demo not available!")
            return

        # Start a timer for the demo
        time_thread = threading.Thread(target=self.sleep_for, args=(demo_duration,))
        time_thread.start()
        threshold_ee = 0.1
        
        # Initialize the targets to be filled in later
        targets: Dict[str, Target] = { 
            'ur5right' : Target(), 
            'ur5left' : Target(), 
            'base' : Target() 
        }
        # Get the device instances from the robot
        ur5right = self.robot.get_device('ur5right')
        ur5left = self.robot.get_device('ur5left')
        
        # counters/indexers used to keep track of waypoints
        right_wp_idx = 0
        left_wp_idx = 0
        while self.timer_running:
            # Set the target values for the robot's devices
            targets['ur5right'].set_xyz(right_wps[right_wp_idx])
            targets['ur5left'].set_xyz(left_wps[left_wp_idx])
            # targets['base'].abg[2] = 0.0
            
            # Generate an OSC signal to steer robot toward the targets
            ctrlr_output = self.controller.generate(targets)
            
            # Generate an OSC signal to steer robot toward the targets
            for force_idx, force  in zip(*ctrlr_output):
                self.sim.data.ctrl[force_idx] = force
            
            # Collect errors for the arms in order to determine whether to update
            # waypoint indexes
            self.errors['ur5right'] = np.linalg.norm(ur5right.get_state(DeviceState.EE_XYZ) - targets['ur5right'].get_xyz())
            self.errors['ur5left'] = np.linalg.norm(ur5left.get_state(DeviceState.EE_XYZ) - targets['ur5left'].get_xyz())
            if self.errors['ur5right']  < threshold_ee:
                if right_wp_idx < right_wps.shape[0] - 1:
                    right_wp_idx += 1
                else:
                    right_wp_idx = 0
            if self.errors['ur5left']  < threshold_ee:
                if left_wp_idx < left_wps.shape[0] - 1:
                    left_wp_idx += 1
                else:
                    left_wp_idx = 0

            # Move the target objects to the new waypoints
            self.sim.data.set_mocap_pos('target_red', right_wps[right_wp_idx])
            self.sim.data.set_mocap_pos('target_blue', left_wps[left_wp_idx])
            
            # Step simulator / Render scene
            self.sim.step()
            self.viewer.render()
        
        # Join threads / Stop the simulator 
        time_thread.join()
        self.robot.stop()
        self.robot_data_thread.join()

# Main entrypoint
if __name__ == "__main__":
    # Initialize the gain test demo
    demo = GainTest(robot_config_file="default_xyz.yaml", scene_file="gain_test_scene.xml")
    # Run the gain test
    demo_name1 = "gain_test"
    demo.run(demo_name1, 10)
    # demo_name2 = "figure8"
    # demo.run(demo_name2, 10)