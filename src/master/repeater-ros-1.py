#!/usr/bin/env python
import time
import rospy
import roslib
import os
import actionlib
import cv2
import rosbag
import threading
import queue
from sensor_msgs.msg import Image, Joy
from geometry_msgs.msg import Twist
from std_msgs.msg import Float64
from bearnav2.msg import MapRepeaterAction, MapRepeaterResult, SensorsInput, SensorsOutput, ImageList, FeaturesList, Features
from bearnav2.srv import SetDist, SetClockGain, SetClockGainResponse, Alignment, Representations
import numpy as np
import ros_numpy


def parse_camera_msg(msg):
    img = ros_numpy.numpify(msg)
    if "bgr" in msg.encoding:
        img = img[..., ::-1]  # switch from bgr to rgb
    img_msg = ros_numpy.msgify(Image, img, "rgb8")
    return img_msg


def load_map(mappaths, images, distances, trans, times):
    if "," in mappaths:
        mappaths = mappaths.split(",")
    for mappath in mappaths:
        tmp = []
        for file in list(os.listdir(mappath)):
            if file.endswith(".npy"):
                tmp.append(file[:-4].split("_"))
        rospy.logwarn(str(len(tmp)) + " images found in the map")
        tmp.sort(key=lambda x: float(x[0]))
        last_time = None
        tmp_images = []
        tmp_distances = []
        tmp_trans = []
        tmp_times = []
        for idx, dist_turn_time in enumerate(tmp):

            tmp_distances.append(float(dist_turn_time[0]))
            # img = cv2.imread(os.path.join(mappath, dist_turn_time[0] + "_" + dist_turn_time[1] + ".jpg"))
            # img_msg = ros_numpy.msgify(Image, img, "rgb8")
            feature = Features()
            with open(os.path.join(mappath, dist_turn_time[0] + "_" + dist_turn_time[1] + "_" + dist_turn_time[2] + ".npy"), 'rb') as fp:
                array = np.load(fp, allow_pickle=False, fix_imports=False)
                feature.shape = array.shape
                feature.values = array.flatten()
            tmp_images.append(feature)
            rospy.loginfo("Loaded feature: " + dist_turn_time[0] + "_" + dist_turn_time[1] + "_" + dist_turn_time[2] + str(".npy"))
            if idx > 0:
                tmp_trans.append(float(dist_turn_time[1]))
                tmp_times.append(int(dist_turn_time[2]) - int(last_time))
            last_time = dist_turn_time[2]
        tmp_times[-1] = tmp_times[-2]  # to avoid very long period before map end
        images.append(tmp_images)
        if len(distances) > 0:
            # TODO remove hotfix
            last_dist = distances[0][-1]
            curr_last_dist = tmp_distances[-1]
            frac = last_dist / curr_last_dist
            tmp_distances = list(np.array(tmp_distances) * frac)
        distances.append(tmp_distances)
        trans.append(tmp_trans)
        times.append(tmp_times)
        rospy.logwarn("Whole map " + str(mappath) + " sucessfully loaded")


class ActionServer():

    def __init__(self):

        #some vars
        self.img = None
        self.mapName = ""
        self.mapStep = None
        self.nextStep = 0
        self.bag = None
        self.isRepeating = False
        self.fileList = []
        self.endPosition = 1.0
        self.clockGain = 1.0
        self.curr_dist = 0.0
        self.map_images = []
        self.map_distances = []
        self.action_dists = None
        self.map_times = []
        self.actions = []
        self.map_publish_span = 1
        self.map_transitions = []
        self.use_distances = False
        self.distance_finish_offset = 0.2
        self.last_nearest_idx = 0
        self.curr_map = 0
        self.map_num = 0
        self.last_map = 0

        rospy.logdebug("Waiting for services to become available...")
        rospy.wait_for_service("repeat/set_dist")
        rospy.wait_for_service("repeat/set_align")
        rospy.Service('set_clock_gain', SetClockGain, self.setClockGain)

        rospy.logdebug("Resetting distance node")
        self.distance_reset_srv = rospy.ServiceProxy("repeat/set_dist", SetDist)
        self.align_reset_srv = rospy.ServiceProxy("repeat/set_align", SetDist)
        self.distance_sub = rospy.Subscriber("repeat/output_dist", SensorsOutput, self.distanceCB, queue_size=1)

        rospy.logdebug("Connecting to sensors module")
        self.sensors_pub = rospy.Publisher("map_representations", SensorsInput, queue_size=1)

        rospy.logdebug("Setting up published for commands")
        self.joy_topic = "map_vel"
        self.joy_pub = rospy.Publisher(self.joy_topic, Twist, queue_size=1)

        rospy.logdebug("Starting repeater server")
        self.server = actionlib.SimpleActionServer("repeater", MapRepeaterAction, execute_cb=self.actionCB, auto_start=False)
        self.server.register_preempt_callback(self.checkShutdown)
        self.server.start()

        rospy.logwarn("Repeater started, awaiting goal")

    def setClockGain(self, req):
        self.clockGain = req.gain 
        return SetClockGainResponse()

    def pubSensorsInput(self):
        # rospy.logwarn("Obtained image!")
        if not self.isRepeating:
            return
        if len(self.map_images) > 0:
            # rospy.logwarn(self.map_distances)
            # Load data from the map
            nearest_main_map_idx = np.argmin(abs(self.curr_dist - np.array(self.map_distances[self.curr_map])))
            if nearest_main_map_idx == self.last_nearest_idx and nearest_main_map_idx != 0 and self.curr_map == self.last_map:
                return
            rospy.loginfo("matching image " + str(nearest_main_map_idx) + " at distance " + str(self.curr_dist))
            # allow only move in map by one image per iteration
            # nearest_map_idx = self.last_nearest_idx + np.sign(nearest_map_idx - self.last_nearest_idx)
            lower_bound = max(0, nearest_main_map_idx - self.map_publish_span)
            upper_bound = min(nearest_main_map_idx + self.map_publish_span + 1, len(self.map_distances[self.curr_map]))
            map_imgs = self.map_images[self.curr_map][lower_bound:upper_bound]
            distances = self.map_distances[self.curr_map][lower_bound:upper_bound]
            if len(self.map_transitions) > 0:
                transitions = self.map_transitions[self.curr_map][lower_bound:upper_bound - 1]
                time_trans = self.map_times[self.curr_map][lower_bound:upper_bound - 1]
            else:
                transitions = []
                time_trans = []
            if self.map_num > 0:
                for map_idx in [i for i in range(self.map_num)]:
                    nearest_map_idx = np.argmin(abs(self.curr_dist - np.array(self.map_distances[map_idx])))
                    map_imgs.append(self.map_images[map_idx][nearest_map_idx])
            # Create message for estimators
            sns_in = SensorsInput()
            sns_in.header.stamp = rospy.Time.now()
            sns_in.map_features = map_imgs
            sns_in.live_features = []
            sns_in.map_distances = distances
            sns_in.map_transitions = transitions
            sns_in.time_transitions = time_trans
            sns_in.maps = [self.curr_map, self.map_num]

            # rospy.logwarn("message created")
            self.sensors_pub.publish(sns_in)
            self.last_nearest_idx = nearest_main_map_idx
            self.last_map = self.curr_map
            
            # rospy.logwarn("Image published!")
            # DEBUGGING
            # self.debug_map_img.publish(self.map_images[nearest_map_idx])

    def distanceCB(self, msg):
        if self.isRepeating == False:
            return
        
        # if self.img is None:
        #     rospy.logwarn("Warning: no image received")

        self.curr_dist = msg.output
        self.curr_map = msg.map

        if self.curr_dist >= (self.map_distances[self.curr_map][-1] - self.distance_finish_offset) and self.use_distances or\
                (self.endPosition != 0.0 and self.endPosition < self.curr_dist):
            rospy.logwarn("GOAL REACHED, STOPPING REPEATER")
            self.isRepeating = False
            if self.use_distances:
                self.action_dists = []
                self.actions = []
            self.shutdown()

        if self.use_distances:
            self.play_closest_action()

        self.pubSensorsInput()

    def goalValid(self, goal):
        
        if goal.mapName == "":
            rospy.logwarn("Goal missing map name")
            return False
        # if not os.path.isdir(goal.mapName):
        #     rospy.logwarn("Can't find map directory")
        #     return False
        # if not os.path.isfile(os.path.join(goal.mapName, goal.mapName + ".bag")):
        #     rospy.logwarn("Can't find commands")
        #     return False
        # if not os.path.isfile(os.path.join(goal.mapName, "params")):
        #     rospy.logwarn("Can't find params")
        #     return False
        if goal.startPos < 0:
            rospy.logwarn("Invalid (negative) start position). Use zero to start at the beginning") 
            return False
        if goal.startPos > goal.endPos:
            rospy.logwarn("Start position greater than end position")
            return False
        return True

    def actionCB(self, goal):

        rospy.loginfo("New goal received")
        result = MapRepeaterResult()       
        if self.goalValid(goal) == False:
            rospy.logwarn("Ignoring invalid goal")
            result.success = False
            self.server.set_succeeded(result)
            return

        map_name = goal.mapName.split(",")[0]
        self.parseParams(os.path.join(map_name, "params"))

        self.map_publish_span = int(goal.imagePub)

        #set distance to zero
        rospy.logdebug("Resetting distnace and alignment")
        self.align_reset_srv(0.0)
        self.endPosition = goal.endPos
        self.nextStep = 0

        # reload all the buffers
        self.map_images = []
        self.map_distances = []
        self.action_dists = None
        self.actions = []
        self.map_transitions = []
        self.last_closest_idx = 0

        map_loader = threading.Thread(target=load_map, args=(goal.mapName, self.map_images, self.map_distances,
                                                             self.map_transitions, self.map_times))
        map_loader.start()
        map_loader.join()
        self.map_num = len(self.map_images)

        rospy.logwarn("Starting repeat")
        self.bag = rosbag.Bag(os.path.join(map_name, map_name + ".bag"), "r")
        self.mapName = goal.mapName
        self.use_distances = goal.useDist
    
        #create publishers
        additionalPublishers = {}
        rospy.logwarn(self.savedOdomTopic)
        for topic, message, ts in self.bag.read_messages():
            if topic is not self.savedOdomTopic:
                topicType = self.bag.get_type_and_topic_info()[1][topic][0]
                topicType = roslib.message.get_message_class(topicType)
                additionalPublishers[topic] = rospy.Publisher(topic, topicType, queue_size=1) 

        self.distance_reset_srv(goal.startPos)
        self.curr_dist = goal.startPos
        time.sleep(2)       # waiting till some map images are parsed
        self.isRepeating = True
        # kick into the robot at the beggining:
        rospy.loginfo("Repeating started!")
        if self.use_distances:
            self.parse_rosbag()
            self.play_closest_action()
        else:
            self.replay_timewise(additionalPublishers)    # for timewise repeating

        # self.shutdown() only for sth
        result.success = True
        self.server.set_succeeded(result)
         
    def parseParams(self, filename):

        with open(filename, "r") as f:
            data = f.read()
        data = data.split("\n")
        data = filter(None, data)
        for line in data:
            line = line.split(" ")
            if "stepSize" in line[0]:
                rospy.logdebug("Setting step size to: %s" % (line[1]))
                self.mapStep = float(line[1])
            if "odomTopic" in line[0]:
                rospy.logdebug("Saved odometry topic is: %s" % (line[1]))
                self.savedOdomTopic = line[1] 

    def checkShutdown(self):
        if self.server.is_preempt_requested():
            self.shutdown()

    def shutdown(self):
        self.isRepeating = False
        if self.bag is not None:
            self.bag.close()

    def replay_timewise(self, additionalPublishers):
        #replay bag
        rospy.logwarn("Starting")
        previousMessageTime = None
        expectedMessageTime = None
        start = rospy.Time.now()
        for topic, message, ts in self.bag.read_messages():
            #rosbag virtual clock
            now = rospy.Time.now()
            if previousMessageTime is None:
                previousMessageTime = ts
                expectedMessageTime = now
            else:
                simulatedTimeToGo = ts - previousMessageTime
                correctedSimulatedTimeToGo = simulatedTimeToGo * self.clockGain
                error = now - expectedMessageTime
                sleepTime = correctedSimulatedTimeToGo - error
                expectedMessageTime = now + sleepTime
                rospy.sleep(sleepTime)
                previousMessageTime = ts
            #publish
            if topic == "recorded_actions":
                self.joy_pub.publish(message.twist)
            else:
                additionalPublishers[topic].publish(message)
            msgBuf = (topic, message)
            if self.isRepeating == False:
                rospy.loginfo("stopped!")
                break
            if rospy.is_shutdown():
                rospy.loginfo("Node Shutdown")
                result = MapRepeaterResult()
                result.success = False
                self.server.set_succeeded(result)
                return
        self.isRepeating = False
        end = rospy.Time.now()
        dur = end - start
        rospy.logwarn("Rosbag runtime: %f" % (dur.to_sec()))

    def parse_rosbag(self):
        rospy.logwarn("Starting to parse the actions")
        self.action_dists = []
        self.action_times = []
        for topic, msg, t in self.bag.read_messages(topics=["recorded_actions"]):
            self.action_dists.append(float(msg.distance))
            self.actions.append(msg.twist)
        self.action_dists = np.array(self.action_dists)
        rospy.logwarn("Actions and distances successfully loaded!")

    def play_closest_action(self):
        # TODO: Does not support additional topics
        if len(self.action_dists) > 0:
            distance_to_pos = abs(self.curr_dist - self.action_dists)
            closest_idx = np.argmin(distance_to_pos)
            rospy.loginfo("replaying action at: " + str(closest_idx))
            if self.isRepeating:
                self.joy_pub.publish(self.actions[closest_idx])
        else:
            rospy.logwarn("No action available")


if __name__ == '__main__':

    rospy.init_node("replayer_server")
    server = ActionServer()
    rospy.spin()
    server.shutdown()
