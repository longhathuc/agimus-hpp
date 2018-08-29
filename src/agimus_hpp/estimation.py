#!/usr/bin/env python
import rospy, hpp.corbaserver
from .client import HppClient
from dynamic_graph_bridge_msgs.msg import Vector
from geometry_msgs.msg import TransformStamped
from sensor_msgs.msg import JointState
from std_msgs.msg import Empty
from math import cos, sin
from threading import Lock
import traceback

class Estimation(HppClient):
    subscribersDict = {
            "estimation": {
                "request" : [Empty, "estimation" ],
                },
            "vision": {
                "tags": [TransformStamped, "get_visual_tag"],
                },
            }
    publishersDict = {
            "estimation": {
                # "estimation"          : [ Vector, 1],
                "semantic_estimation" : [ Vector, 1],
                },
            }

    def __init__ (self):
        super(Estimation, self).__init__ (postContextId = "_estimation")

        self.subscribers = self._createTopics ("/agimus", self.subscribersDict, True)
        self.publishers = self._createTopics ("/agimus", self.publishersDict, False)
        self.joint_state_subs = rospy.Subscriber ("/joint_states", JointState, self.get_joint_state)

        self.setHppUrl()

        self.mutex = Lock()

        self.robot_name = rospy.get_param("robot_name", "")

        self.last_stamp = rospy.Time.now()
        self.last_visual_tag_constraints = list()

        self.current_stamp = rospy.Time.now()
        self.current_visual_tag_constraints = list()

    def estimation (self, msg):
        hpp = self._hpp()
        self.mutex.acquire()

        try:
            q_current = hpp.robot.getCurrentConfig()

            self._initialize_constraints (q_current)

            success, q_projected, error = hpp.problem.applyConstraints (q_current)

            if success:
                success, q_estimated, error = hpp.problem.optimize (q_projected)
            else:
                q_estimated = q_projected
                rospy.logwarn ("Could not apply the constraints {0}".format(error))

            rospy.loginfo ("At {0}, estimated {1}".format(self.last_stamp, q_estimated))
            rospy.loginfo ("Success: {0}. error {1}".format(success, error))

            valid, msg = hpp.robot.isConfigValid (q_estimated)
            if not valid:
                rospy.logwarn ("Estimation in collision: {0}".format(msg))

            self.publishers["estimation"]["semantic_estimation"].publish (q_estimated)

            # TODO publish in tf to enable vizualisation in rviz
        except Exception as e:
            rospy.logerr (str(e))
            rospy.logerr (traceback.format_exc())
        finally:
            self.mutex.release()

    def _initialize_constraints (self, q_current):
        from CORBA import UserException
        hpp = self._hpp()

        hpp.problem.resetConstraints()

        if hasattr(self, "manip"): # hpp-manipulation:
            # Guess current state
            manip = self._manip ()
            try:
                state_id = manip.graph.getNode (q_current)
                rospy.loginfo("At {0}, current state: {1}".format(self.last_stamp, state_id))
            except UserException:
                state_id = rospy.get_param ("default_state_id")
                rospy.logwarn("At {0}, assumed default current state: {1}".format(self.last_stamp, state_id))

            # copy constraint from state
            manip.problem.setConstraints (state_id, True)
        else:
            # hpp-corbaserver: setNumericalConstraints
            default_constraints = rospy.get_param ("default_constraints")
            hpp.problem.addNumericalConstraints ("constraints",
                    default_constraints,
                    [ 0 for _ in default_constraints ])

        # TODO we should solve the constraints, then add the cost and optimize.
        # TODO Add a configuration constraint fed with the configuration from joint state topic
        rospy.loginfo("Adding {0}".format(self.last_visual_tag_constraints))
        hpp.problem.addNumericalConstraints ("unused",
                self.last_visual_tag_constraints,
                [ 1 for _ in self.last_visual_tag_constraints ])
        hpp.problem.setNumericalConstraintsLastPriorityOptional (True)

    def get_joint_state (self, js_msg):
        self.mutex.acquire()
        try:
            hpp = self._hpp()
            for jn, q in zip(js_msg.name, js_msg.position):
                size = hpp.robot.getJointConfigSize(self.robot_name + jn)
                if size == 2:
                    hpp.robot.setJointConfig(self.robot_name + jn, [cos(q), sin(q)])
                else:
                    hpp.robot.setJointConfig(self.robot_name + jn, [q])
        finally:
            self.mutex.release()

    def get_visual_tag (self, ts_msg):
        stamp = ts_msg.header.stamp
        if stamp < ts_msg.header.current_stamp: return
        self.mutex.acquire()
        try:
            hpp = self._hpp()

            # Create a relative transformation constraint
            j1 = ts_msg.header.frame_id
            j2 = ts_msg.child_frame_id
            name = j1 + "_" + j2
            T = [ ts_msg.transform.translation.x,
                  ts_msg.transform.translation.y,
                  ts_msg.transform.translation.z,
                  ts_msg.transform.rotation.x,
                  ts_msg.transform.rotation.y,
                  ts_msg.transform.rotation.z,
                  ts_msg.transform.rotation.w,]
            hpp.problem.createTransformationConstraint (name, j1, j2, T, [True,]*6)

            # If this tag is in the next image:
            if self.current_stamp > stamp:
                # Assume no more visual tag will be received from image at time current_stamp.
                self.last_stamp = self.current_stamp
                self.last_visual_tag_constraints = self.current_visual_tag_constraints
                # Reset for next image.
                self.current_stamp = stamp
                self.current_visual_tag_constraints = list()
            self.current_visual_tag_constraints.append(name)

        finally:
            self.mutex.release()
