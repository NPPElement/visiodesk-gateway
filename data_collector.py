import json
import logging
import time
import traceback
import os
from pathlib import Path
from subprocess import TimeoutExpired
from threading import Thread
from random import randint, shuffle
import argparse
import queue

import config.visiobas
from bacnet.bacnet import ObjectProperty, StatusFlags, StatusFlag, ObjectType
from bacnet.parser import BACnetParser
from bacnet.slicer import BACnetSlicer
from visiobas.gate_client import VisiobasGateClient
from visiobas.object.bacnet_object import BACnetObject, Device, NotificationClass, Transition
from visiobas import visiodesk
from bacnet.network import BACnetNetwork
import config.logging
from visiobas.visiodesk import TopicType

bacnet_network = BACnetNetwork()


class Statistic(Thread):
    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger("visiobas.data_collector.statistic")
        self.count_read_objects = 0
        self.count_verified_objects = 0
        self.count_notified_objects = 0
        self.count_send_objects = 0
        self.count_send_queue = 0
        self.duration_read_objects_sec = 0
        self.duration_verify_objects_sec = 0
        self.duration_notified_objects_sec = 0
        self.duration_send_objects_sec = 0
        self.duration_notified_objects = 0
        self.devices_not_responding = set([])

    def add_not_responding_device(self, device_id):
        self.devices_not_responding.add(device_id)

    def remove_not_responding_device(self, device_id):
        if device_id in self.devices_not_responding:
            self.devices_not_responding.remove(device_id)

    def enabled(self):
        return self.logger.isEnabledFor(logging.INFO)

    def update_read_object_statistic(self, inc, duration):
        self.count_read_objects += inc
        self.duration_read_objects_sec += duration

    def update_verified_object_statistic(self, inc, duration):
        self.count_verified_objects += inc
        self.duration_verify_objects_sec += duration

    def update_send_object_statistic(self, inc, left, duration):
        self.count_send_objects += inc
        self.count_send_queue = left
        self.duration_send_objects_sec += duration

    def update_notified_object_statistic(self, inc, duration):
        self.count_notified_objects += inc
        self.duration_notified_objects += duration

    def print_statistic(self):
        if self.count_read_objects == 0:
            return

        read_rate = self.count_read_objects / self.duration_read_objects_sec
        verify_rate = self.count_verified_objects / self.duration_verify_objects_sec
        send_rate = self.count_send_objects / self.duration_send_objects_sec

        self.logger.info("\nread ............. {}     , total duration: {:.2f} sec, rate {:.2f} objects / sec"
                         "\nverify ........... {}     , total duration: {:.2f} sec, rate {:.2f} objects / sec"
                         "\nsend ............. {} ({}), total durationL {:.2f} sec, rate {:.2f} objects / sec".format(
            self.count_read_objects, self.duration_read_objects_sec, read_rate,
            self.count_verified_objects, self.duration_verify_objects_sec, verify_rate,
            self.count_send_objects, self.count_send_queue, self.duration_send_objects_sec, send_rate))
        if not len(self.devices_not_responding) == 0:
            self.logger.info("NOT responding devices: {}".format(self.devices_not_responding))

    def run(self) -> None:
        while (True):
            time.sleep(10)
            try:
                self.print_statistic()
            except:
                pass


statistic = Statistic()
statistic.setDaemon(True)
statistic.start()


class VisiobasTransmitter(Thread):

    def __init__(self, gate_client, period: int = 0.1):
        """
        :param gate_client:
        :param period: time in seconds waiting new data before send collected data to server
        """
        super().__init__()
        # global collected data should be transmitted to server
        self.device_ids = []
        # self.collected_data = {}
        self.collected_queue = queue.Queue(10000)
        self.gate_client = gate_client
        self.period = period
        self.logger = logging.getLogger('visiobas.data_collector.transmitter')
        self.send_fields = [
            ObjectProperty.OBJECT_TYPE.id(),
            ObjectProperty.OBJECT_IDENTIFIER.id(),
            ObjectProperty.PRESENT_VALUE.id(),
            ObjectProperty.STATUS_FLAGS.id(),
            ObjectProperty.PRIORITY_ARRAY.id()
        ]
        self.enabled = True
        self.max_objects_per_request = 50
        self.heart_beat = time.time()

    def set_enable(self, enabled):
        self.enabled = enabled

    def push_collected_data(self, bacnet_object: BACnetObject):
        if not self.enabled:
            return
        try:
            # _id = data[ObjectProperty.OBJECT_IDENTIFIER.id()]
            # _device_id = data[ObjectProperty.DEVICE_ID.id()]
            key = bacnet_object.get_object_reference()
            device_id = bacnet_object.get_device_id()
            # self.collected_data[key] = bacnet_object
            if self.collected_queue.full():
                # free the most latests data and put append queue with new one
                self.collected_queue.get()
            self.collected_queue.put(bacnet_object)
            if device_id not in self.device_ids:
                self.device_ids.append(device_id)
        except:
            self.logger.exception("Failed put collected data: {}".format(bacnet_object))

    def __make_request(self, device_id, request):
        sended = len(request)
        if sended == 0:
            return
        t0 = time.time()
        success = True
        try:
            rejected = self.gate_client.rq_put(device_id, request)
            for o in rejected:
                self.logger.error("Rejected: {}".format(o))
            if not len(rejected) == 0:
                sended -= len(rejected)
                self.logger.error("Rejected request: {}".format(json.dumps(request)))
        except Exception as e:
            success = False
            self.logger.exception("Failed put batch of data: {}".format(json.dumps(request)))

        if not success:
            self.logger.info("Trying to put one by one...")
            for d in request:
                try:
                    rejected = self.gate_client.rq_put(device_id, [d])
                    for o in rejected:
                        sended -= 1
                        self.logger.error("Rejected: {}".format(o))
                        self.logger.error("Rejected request: {}".format(json.dumps([d])))
                except:
                    self.logger.exception("Failed put data: {}".format(json.dumps([d])))
        if statistic.enabled():
            statistic.update_send_object_statistic(sended, self.collected_queue.qsize(), time.time() - t0)

    def run(self) -> None:
        while True:
            self.heart_beat = time.time()
            try:
                send_list = []
                t0 = time.time()
                while len(send_list) < self.max_objects_per_request and (time.time() - t0) < 2:
                    try:
                        bacnet_object = self.collected_queue.get(True, 1)
                        data = {}
                        for field in self.send_fields:
                            if field == ObjectProperty.PRIORITY_ARRAY.id():
                                priority = bacnet_object.get(field)
                                if type(priority) == list and len(priority) == 16:
                                    data[field] = priority
                            else:
                                data[field] = bacnet_object.get(field)
                        send_list.append((bacnet_object.get_device_id(), data))
                    except queue.Empty:
                        pass

                request_device_id = -1
                request = []
                while not len(send_list) == 0:
                    device_id, data = send_list.pop()
                    if request_device_id == -1:
                        request_device_id = device_id
                    if request_device_id == device_id:
                        request.append(data)
                    else:
                        send_list.append((device_id, data))
                    if len(send_list) == 0 or not request_device_id == device_id:
                        self.__make_request(request_device_id, request)
                        request = []
                        request_device_id = -1
            except:
                self.logger.exception("Failed data transmit")


class VisiobasNotifier(Thread):
    def __init__(self,
                 client: VisiobasGateClient,
                 bacnet_network: BACnetNetwork):
        super().__init__()
        self.client = client
        self.bacnet_network = bacnet_network
        self.transitions = {}
        self.logger = logging.getLogger('visiobas.data_collector.notifier')
        self.notification_groups = {}
        self.enabled = True
        self.topic_id_cache = {}
        self.group_id_cache = {}
        self.description_cache = {}
        self.heart_beat = time.time()
        # self.notification_group_name = notification_group_name
        # self.notification_group_id = self.__find_notification_group_id()

    def set_enable(self, enable):
        self.enabled = enable

    def get_notification_recipients(self, notification_class_id):
        try:
            notification_class = self.bacnet_network.find_by_type(ObjectType.NOTIFICATION_CLASS, notification_class_id)
            return notification_class.get_recipient_list()
        except:
            self.logger.error("Failed get notification group")
        return None

    def push_transitions(self, bacnet_object: BACnetObject, transition: Transition):
        if not self.enabled:
            return
        try:
            key = bacnet_object.get_object_reference() + "_" + str(transition)
            self.transitions[key] = (bacnet_object, transition)
        except:
            self.logger.exception("Failed put collected data: {}".format(bacnet_object))

    @staticmethod
    def __is_reference(reference: str):
        return reference.startswith("Site:")

    @staticmethod
    def __is_system_topic_item_text(text: str):
        return text.startswith("~System Notification~")

    @staticmethod
    def __create_system_topic_item_text(group_name, bacnet_object, transition):
        return "\n".join([
            "~System Notification~",
            "Group: {}".format(group_name),
            "Reference: {}".format(bacnet_object.get_object_reference()),
            "Transition: {}".format(transition)
        ])

    @staticmethod
    def __decode_system_topic_item_text(text):
        group_name = -1
        reference = ""
        transition = None
        for line in text.split("\n"):
            if line.startswith("Group:"):
                group_name = line[len("Group:"):].strip()
            elif line.startswith("Reference:"):
                reference = line[len("Reference:"):].strip()
            elif line.startswith("Transition:"):
                transition = line[len("Transition:"):].strip()
        return {
            "group_name": group_name,
            "reference": reference,
            "transition": transition
        }

    def __create_topic_id_cache_key(self, group_name, bacnet_object, transition):
        transition = Transition.TO_FAULT if transition == Transition.RESOLVE_FAULT else transition
        transition = Transition.TO_OFFNORMAL if transition == Transition.RESOLVE_OFFNORMAL else transition
        reference = bacnet_object.get_object_reference()
        return group_name + "_" + str(transition) + "_" + reference

    def __find_topic_id(self, group_name, bacnet_object, transition):
        transition = Transition.TO_FAULT if transition == Transition.RESOLVE_FAULT else transition
        transition = Transition.TO_OFFNORMAL if transition == Transition.RESOLVE_OFFNORMAL else transition
        reference = bacnet_object.get_object_reference()

        try:
            key = self.__create_topic_id_cache_key(group_name, bacnet_object, transition)
            if key in self.topic_id_cache:
                # TODO does topic still exist on server?
                return self.topic_id_cache[key]

            topics = self.client.rq_vdesk_get_topic_by_user()
            # TODO update API filter by group ? - need to find topic for write notification about FAILED sensor point
            for topic in topics:
                items = topic["items"]
                for item in items:
                    if item["type"]["id"] == visiodesk.ItemType.MESSAGE.id():
                        if "text" in item:
                            if self.__is_system_topic_item_text(item["text"]):
                                decoded = self.__decode_system_topic_item_text(item["text"])
                                if decoded["group_name"] == group_name \
                                        and decoded["reference"] == reference \
                                        and decoded["transition"] == str(transition):
                                    self.topic_id_cache[key] = topic["id"]
                                    return topic["id"]
            return None
        except:
            self.logger.exception("Failed find topic group: {} reference: {}".format(group_name, reference))
            return None

    def __find_notification_group_id(self, group_name) -> int:
        try:
            if group_name in self.group_id_cache:
                return self.group_id_cache[group_name]
            groups = self.client.rq_vdesk_get_groups()
            found = next(filter(lambda g: g["name"] == group_name, groups), None)
            group_id = found['id'] if found is not None else 0
            if not group_id == 0:
                self.group_id_cache[group_name] = group_id
            return group_id
        except:
            self.logger.exception("Failed find group: {}".format(group_name))
            return 0

    def __create_topic(self, group_name: str, bacnet_object: BACnetObject, transition: Transition, topic_type: int):
        group_id = self.__find_notification_group_id(group_name)
        if group_id == 0:
            self.logger.error("Failed find notification group: {}".format(group_name))
            return

        description = bacnet_object.get_description()
        reference = bacnet_object.get_object_reference()

        # родительская папка (description родительской папки)
        topic_title = self.__find_topic_title(bacnet_object)
        topic_title = topic_title if topic_title else reference

        # аварийный текст "Значение вышло за пределы к"
        topic_description = description
        topic_description = topic_description if topic_description else "[p]{} OUT OF LIMITS[/p]".format(topic_title)
        priority_id = bacnet_object.get_notification_object().get_priority(transition)

        message_text = bacnet_object.get_event_message_text(transition)

        data = {
            "name": topic_title,
            "topic_type": {"id": topic_type},
            "items": [
                {
                    "type": {
                        "id": visiodesk.ItemType.PRIORITY.id()
                    },
                    "priority": {
                        "id": priority_id
                    },
                    "text": visiodesk.TopicPriority.from_id(priority_id).name(),
                    "name": "Повышенный",
                    "like": 0
                },
                {
                    "type": {
                        "id": visiodesk.ItemType.STATUS.id()
                    },
                    "status": {
                        "id": visiodesk.TopicStatus.NEW.id()
                    },
                    "text": visiodesk.TopicStatus.NEW.name(),
                    "name": "Новая",
                    "like": 0
                },
                {
                    "type": {
                        "id": visiodesk.ItemType.MESSAGE.id()
                    },
                    "text": self.__create_system_topic_item_text(group_name, bacnet_object, transition),
                    "name": "Сообщение",
                    "like": 0
                },
                {
                    "type": {
                        "id": visiodesk.ItemType.MESSAGE.id()
                    },
                    "text": message_text,
                    "name": "Сообщение",
                    "like": 0
                }
            ],
            "groups": [{"id": group_id}],
            "description": topic_description
        }
        topic = self.client.rq_vdesk_add_topic(data)
        key = self.__create_topic_id_cache_key(group_name, bacnet_object, transition)
        self.topic_id_cache[key] = topic["id"]
        print(topic)

    def __change_status_if_necessary(self, topic_id, bacnet_object):
        try:
            topic = self.client.rq_vdesk_get_topic_by_id(topic_id)
            items = topic["items"]
            last_status_id = None
            for item in items:
                if item["type"]["id"] == visiodesk.ItemType.STATUS.id():
                    last_status_id = item["status"]["id"]
            if last_status_id == visiodesk.TopicStatus.RESOLVED.id():
                self.client.rq_vdesk_add_topic_items([{
                    "type": {
                        "id": visiodesk.ItemType.STATUS.id()
                    },
                    "status": {
                        "id": visiodesk.TopicStatus.NEW.id()
                    },
                    "text": visiodesk.TopicStatus.NEW.name(),
                    "name": "Новая",
                    "like": 0,
                    "topic": {
                        "id": topic["id"]
                    }
                }])
        except:
            self.logger.exception("Failed change topic status, topic id: {} object: {}".format(topic_id, bacnet_object))

    def __create_notification(self, bacnet_object: BACnetObject, transition: Transition):
        notification_class = bacnet_object.get_notification_object()
        # self.logger.error("NOTIFICATION {} {}".format(bacnet_object, transition))
        if not notification_class:
            return
        recipients = notification_class.get_recipient_list()
        for recipient in recipients:
            group_name = recipient["recipient"] if "recipient" in recipient else None
            notification_transition_allows = recipient["transitions"] if "transitions" in recipient else None
            if not group_name or not notification_transition_allows:
                # self.logger.error("NOTIFICATION group empty")
                continue
            if not bacnet_object.is_notification_allowed(transition):
                # self.logger.error("NOTIFICATION not allowed")
                continue
            if not notification_transition_allows[transition.id()]:
                # self.logger.error("NOTIFICATION transition not allowed")
                continue

            topic_id = self.__find_topic_id(group_name, bacnet_object, transition)
            if topic_id is None:
                # this cases should be always False but for any case
                if transition == Transition.RESOLVE_OFFNORMAL:
                    continue
                if transition == Transition.RESOLVE_FAULT:
                    continue
                topic_type = recipient["topic_type"] if "topic_type" in recipient else [TopicType.REQUEST.id(),
                                                                                        TopicType.REQUEST.id(),
                                                                                        TopicType.REQUEST.id()]
                topic_type = topic_type[transition.id()]
                self.logger.info("Create topic: {} object: {} {}".format(group_name, bacnet_object, transition))
                self.__create_topic(group_name, bacnet_object, transition, topic_type)
            else:
                self.__append_transition_text_into_topic(topic_id, bacnet_object, transition)
                # self.__change_status_if_necessary(topic_id, bacnet_object)

    # def __create_notification_to_offnormal(self, bacnet_object: BACnetObject, notification_class: NotificationClass):
    #     topic = self.__find_topic_id(bacnet_object.get_object_reference())
    #     if topic is None:
    #         recipient_list = notification_class.get_recipient_list()
    #         for recipient in recipient_list:
    #             try:
    #                 group_name = recipient["recipient"]
    #                 # TODO verify transition flags also
    #                 group_id = self.__find_notification_group_id(group_name)
    #                 if not group_id == 0:
    #                     self.__create_topic(group_id, bacnet_object)
    #             except:
    #                 self.logger.error(traceback.format_exc())
    #     else:
    #         self.__change_status_if_necessary(topic, bacnet_object)

    def run(self) -> None:
        while True:
            self.heart_beat = time.time()
            keys = list(self.transitions.keys())
            for key in keys:
                try:
                    bacnet_object, transition = self.transitions.pop(key)
                    self.__create_notification(bacnet_object, transition)
                except:
                    self.logger.exception("Failed create notification of: {}".format(key))

            time.sleep(1)

    def __find_topic_title(self, bacnet_object):
        try:
            reference = bacnet_object.get_object_reference()
            parent_reference = "/".join(self.client.reference_as_list(reference)[:-1])
            if parent_reference in self.description_cache:
                return self.description_cache[parent_reference]
            parent = self.client.rq_vbas_get_object(parent_reference)
            self.description_cache[parent_reference] = parent[ObjectProperty.DESCRIPTION.id()]
            return self.description_cache[parent_reference]
        except:
            self.logger.exception("Failed get topic title: {}".format(bacnet_object))
        return None

    def __append_transition_text_into_topic(self, topic_id: int, bacnet_object: BACnetObject, transition: Transition):
        try:
            self.client.rq_vdesk_add_topic_items([{
                "type": {
                    "id": visiodesk.ItemType.MESSAGE.id()
                },
                "text": bacnet_object.get_event_message_text(transition),
                "like": 0,
                "topic": {
                    "id": topic_id
                }
            }])
        except:
            self.logger.exception("Failed append transition text, topic: {} object: {}".format(topic_id, bacnet_object))


class VisiobasDataVerifier(Thread):
    def __init__(self,
                 client: VisiobasGateClient,
                 transmitter: VisiobasTransmitter,
                 notifier: VisiobasNotifier,
                 bacnet_network: BACnetNetwork):
        super().__init__()
        self.logger = logging.getLogger('visiobas.data_collector.verifier')
        self.transmitter = transmitter
        self.notifier = notifier
        self.collected_data = {}
        self.bacnet_network = bacnet_network
        self.client = client
        self.enabled = True
        self.heart_beat = time.time()

    def set_enable(self, enabled):
        self.enabled = enabled

    def push_collected_data(self, bacnet_object: BACnetObject, data: dict):
        if not self.enabled:
            return
        try:
            key = bacnet_object.get_object_reference()
            self.collected_data[key] = (bacnet_object, data)
            # self.logger.error("PUSH VERIFIER {}".format(key))
        except:
            self.logger.exception("Failed put collected data: {} object: {}".format(data, bacnet_object))

    def verify_analog_object_out_of_limit(self, bacnet_object: BACnetObject, data: dict):
        present_value = data[ObjectProperty.PRESENT_VALUE.id()]
        low_limit = bacnet_object.get_low_limit()
        if low_limit is not None and present_value < low_limit:
            return True
        high_limit = bacnet_object.get_high_limit()
        if high_limit is not None and present_value > high_limit:
            return True
        return False

    def verify_binary_out_of_limit(self, bacnet_object: BACnetObject, data: dict):
        present_value = data[ObjectProperty.PRESENT_VALUE.id()]
        alarm_value = bacnet_object.get_alarm_value()
        return alarm_value == present_value

    def verify_multistate_out_of_limit(self, bacnet_object: BACnetObject, data: dict):
        present_value = data[ObjectProperty.PRESENT_VALUE.id()]
        alarm_values = bacnet_object.get_alarm_values()
        return present_value in alarm_values

    def verify_object_out_of_limit(self, bacnet_object: BACnetObject, data: dict):
        object_type_code = bacnet_object.get_object_type_code()
        if object_type_code == ObjectType.ANALOG_INPUT.code() or \
                object_type_code == ObjectType.ANALOG_OUTPUT.code() or \
                object_type_code == ObjectType.ANALOG_VALUE:
            return self.verify_analog_object_out_of_limit(bacnet_object, data)
        elif object_type_code == ObjectType.BINARY_INPUT.code() or \
                object_type_code == ObjectType.BINARY_OUTPUT.code() or \
                object_type == ObjectType.BINARY_VALUE.code():
            return self.verify_binary_out_of_limit(bacnet_object, data)
        elif object_type_code == ObjectType.MULTI_STATE_INPUT.code() or \
                object_type_code == ObjectType.MULTI_STATE_OUTPUT.code() or \
                object_type_code == ObjectType.MULTI_STATE_VALUE.code():
            return self.verify_multistate_out_of_limit(bacnet_object, data)

    def verify_to_fault_transition(self, bacnet_object: BACnetObject, data: dict):
        """
        verify bacnet_object state and new collected data
        return new status of FAULT flag required to save into BACnetObject
        and notifier transition if necessary or None
        """
        data_reliability = data[ObjectProperty.RELIABILITY.id()] \
            if ObjectProperty.RELIABILITY.id() in data else "no-fault-detected"
        data_flags = StatusFlags(data[ObjectProperty.STATUS_FLAGS.id()]
                                 if ObjectProperty.STATUS_FLAGS.id() in data else None)

        transition = None
        fault_flag = bacnet_object.get_status_flag(StatusFlag.FAULT)

        # handle TO_FAULT object transition
        if "fault" in data:
            # data point not available, probably it offline
            if not fault_flag:
                fault_flag = True
                transition = Transition.TO_FAULT
        else:
            # verification FAULT flag after object data collection
            if data_flags.get_fault() or not data_reliability == "no-fault-detected":
                if not fault_flag:
                    fault_flag = True
                    transition = Transition.TO_FAULT
            elif fault_flag:
                # return FAULT flag to normal after object restore data collection for instance
                fault_flag = False
                transition = Transition.RESOLVE_FAULT
        return fault_flag, transition

    def verify_to_offnormal_transition(self, bacnet_object: BACnetObject, data: dict):
        """
        verify bacnet_object state and new collected data
        return new status of IN_ALARM flag required to save into BACnetObject
        and notifier transition if necessary or None
        """
        transition = None
        in_alarm_flag = bacnet_object.get_status_flag(StatusFlag.IN_ALARM)

        is_fault = "fault" in data
        if not is_fault:
            data_flags = StatusFlags(data[ObjectProperty.STATUS_FLAGS.id()]
                                     if ObjectProperty.STATUS_FLAGS.id() in data else None)
            is_fault = data_flags.get_fault()

        # handle TO_ALARM object transition
        if not is_fault:
            # out of limit verification make sense only if current data not fault
            if bacnet_object.get_event_detection_enable():
                is_out_of_limit = self.verify_object_out_of_limit(bacnet_object, data)
                if is_out_of_limit and not in_alarm_flag:
                    in_alarm_flag = True
                    transition = Transition.TO_OFFNORMAL
                elif not is_out_of_limit:
                    if in_alarm_flag:
                        transition = Transition.RESOLVE_OFFNORMAL
                    in_alarm_flag = False
            elif in_alarm_flag:
                # restore IN_ALARM flag if event detection disable
                in_alarm_flag = False

        return in_alarm_flag, transition

    def run(self) -> None:
        while True:
            try:
                self.heart_beat = time.time()
                keys = list(self.collected_data.keys())
                for key in keys:
                    t0 = time.time()
                    bacnet_object = None
                    data = None
                    try:
                        bacnet_object, data = self.collected_data.pop(key)
                        transitions = []
                        flags0 = StatusFlags(bacnet_object.get_status_flags().copy())
                        flags1 = StatusFlags(bacnet_object.get_status_flags().copy())

                        fault_flag, transition = self.verify_to_fault_transition(bacnet_object, data)
                        flags1.set_fault(fault_flag)
                        if transition:
                            transitions.append(transition)

                        in_alarm_flag, transition = self.verify_to_offnormal_transition(bacnet_object, data)
                        flags1.set_in_alarm(in_alarm_flag)
                        if transition:
                            transitions.append(transition)

                        # handle TO_NORMAL object transition
                        if flags0.is_abnormal() and flags1.is_normal():
                            transitions.append(Transition.TO_NORMAL)

                        is_data_fault = "fault" in data
                        if not is_data_fault:
                            data_flags = StatusFlags(data[ObjectProperty.STATUS_FLAGS.id()]
                                                     if ObjectProperty.STATUS_FLAGS.id() in data else None)
                            is_data_fault = data_flags.get_fault()

                        # update state of bacnet_object depend on collected data only if data was not FAULT
                        for property_code in data:
                            if property_code == ObjectProperty.STATUS_FLAGS.id():
                                continue
                            if property_code == ObjectProperty.PRIORITY_ARRAY.id():
                                v = data[property_code]
                                if type(v) == list and len(v) == 16:
                                    bacnet_object.set(property_code, data[property_code])
                            elif property_code == ObjectProperty.PRESENT_VALUE.id():
                                if is_data_fault:
                                    # present value can be invalid when data is fault
                                    continue
                                object_type_code = bacnet_object.get_object_type_code()
                                if object_type_code == ObjectType.ANALOG_INPUT.code() or \
                                        object_type_code == ObjectType.ANALOG_OUTPUT.code() or \
                                        object_type_code == ObjectType.ANALOG_VALUE.code():
                                    bacnet_object.set_present_value(float(data[property_code]))
                                elif object_type_code == ObjectType.MULTI_STATE_INPUT.code() or \
                                        object_type_code == ObjectType.MULTI_STATE_OUTPUT.code() or \
                                        object_type_code == ObjectType.MULTI_STATE_VALUE.code():
                                    bacnet_object.set_present_value(int(float(data[property_code])))
                                elif object_type_code == ObjectType.BINARY_INPUT.code() or \
                                        object_type_code == ObjectType.BINARY_OUTPUT.code() or \
                                        object_type_code == ObjectType.BINARY_VALUE.code():
                                    v = data[property_code]
                                    if type(v) == bool:
                                        v = "active" if v else "inactive"
                                    bacnet_object.set_present_value(v)
                                else:
                                    bacnet_object.set_present_value(data[property_code])
                            else:
                                bacnet_object.set(property_code, data[property_code])

                        # update new state of status flags
                        bacnet_object.set_status_flags(flags1.as_list())
                        # self.logger.error("VERIFIER {} transition {}".format(bacnet_object, transitions))

                        for transition in transitions:
                            self.notifier.push_transitions(bacnet_object, transition)

                        self.transmitter.push_collected_data(bacnet_object)
                    except:
                        self.logger.exception("Failed verify object: {} data: {}".format(bacnet_object, data))
                    if statistic.enabled():
                        duration = time.time() - t0
                        statistic.update_verified_object_statistic(1, duration)
            except:
                self.logger.exception("Failed verifier")
            time.sleep(0.01)


class VisiobasThreadDataCollector(Thread):

    def __init__(self,
                 thread_idx: int,
                 verifier: VisiobasDataVerifier,
                 bacnet_network: BACnetNetwork,
                 period: float = 0.01):
        super().__init__()
        self.thread_idx = thread_idx
        self.data_pooling = {}
        self.verifier = verifier
        self.bacnet_network = bacnet_network
        # self.transmitter = transmitter
        self.logger = logging.getLogger('visiobas.data_collector.collector')
        self.period = period
        self.pooling_fields = [
            ObjectProperty.OUT_OF_SERVICE.id(),
            ObjectProperty.PRESENT_VALUE.id(),
            ObjectProperty.RELIABILITY.id(),
            ObjectProperty.STATUS_FLAGS.id(),
            ObjectProperty.PRIORITY_ARRAY.id()
        ]
        self.analog_pooling_fields = [
            ObjectProperty.OUT_OF_SERVICE.id(),
            ObjectProperty.PRESENT_VALUE.id(),
            ObjectProperty.RELIABILITY.id(),
            ObjectProperty.STATUS_FLAGS.id()
        ]
        self.heart_beat = time.time()

    def add_object(self, bacnet_object: BACnetObject):
        device_id = bacnet_object.get_device_id()
        device = self.bacnet_network.find_by_type(ObjectType.DEVICE, device_id)
        if not device:
            logger.warning("BACnet object does not have device: {}".format(bacnet_object))
            return
        read_app = device.get_read_app() if device is not None else None
        if read_app is None:
            logger.warning("BACnet device does not have read app: {}".format(device))
            return

        if device_id not in self.data_pooling:
            self.data_pooling[device_id] = []

        self.data_pooling[device_id].append({
            "update_interval": bacnet_object.get_update_interval(),
            "original_update_interval": bacnet_object.get_update_interval(),
            "time_last_success_pooling": 0,
            # special delay for make uniform distribute of sensors pooling
            "update_delay": -1,
            "bacnet_object": bacnet_object,
            "read_app": read_app
        })

    def run(self):
        if self.logger.isEnabledFor(logging.INFO):
            count = 0
            for device_id in self.data_pooling:
                count += len(self.data_pooling[device_id])
            self.logger.info("Collector# {} count of observable objects: {}".format(self.thread_idx, count))

        # skip pooling device if it not respond (once fault object response)
        shuffle_data_points_of_device_id = -1
        enable_skip_device = len(self.data_pooling) > 1

        while True:
            try:
                slicer = BACnetSlicer(config.visiobas.visiobas_slicer)
                for device_id in self.data_pooling:
                    data_points = self.data_pooling[device_id]
                    for pooling in data_points:
                        self.heart_beat = time.time()
                        time_last_success_pooling = pooling["time_last_success_pooling"]
                        update_delay = pooling["update_delay"]
                        update_interval = pooling["update_interval"]
                        if time.time() - time_last_success_pooling > update_interval:
                            t0 = time.time()
                            try:
                                # make sensor pooling distributed more uniformed
                                pooling["update_delay"] = randint(1, max(int(update_interval), 1)) \
                                    if update_delay == -1 else 0
                                pooling["update_interval"] = pooling["update_delay"] \
                                    if pooling["update_delay"] > 0 else pooling["original_update_interval"]

                                bacnet_object = pooling["bacnet_object"]

                                object_type_code = bacnet_object.get_object_type_code()
                                object_id = bacnet_object.get_id()
                                read_app = pooling["read_app"]

                                # execute BAC0 or other app
                                _t = time.time()
                                pooling_fields = self.analog_pooling_fields \
                                    if object_type_code == ObjectType.ANALOG_INPUT.code() or \
                                       object_type_code == ObjectType.MULTI_STATE_INPUT.code() or \
                                       object_type_code == ObjectType.BINARY_INPUT.code() \
                                    else self.pooling_fields
                                _dt = 0
                                try:
                                    data = slicer.execute(read_app,
                                                          device_id=device_id,
                                                          object_type=object_type_code,
                                                          object_id=object_id,
                                                          fields=pooling_fields,
                                                          timeout=config.visiobas.visiobas_slicer["read_timeout"])
                                    _dt = time.time() - _t
                                except TimeoutExpired:
                                    _dt = time.time() - _t
                                    data = {}
                                self.last_data_collect = time.time()
                                if len(data) == 0:
                                    logger.error("Failed collect device: {} data of: {} dt: {:.2f}".format(
                                        device_id, bacnet_object.get_object_reference(), _dt))
                                    data["fault"] = True

                                # TODO if data pooling failed? reset last success pooling ?
                                pooling["time_last_success_pooling"] = time.time()

                                self.verifier.push_collected_data(bacnet_object, data)

                                # skip pooling current device and shuffle data points if necessary
                                if "fault" in data and enable_skip_device and \
                                        _dt >= config.visiobas.visiobas_slicer["read_timeout"]:
                                    shuffle_data_points_of_device_id = device_id
                                    break
                            except:
                                logger.exception("Failed execute slice")
                            if statistic.enabled():
                                duration = time.time() - t0
                                statistic.update_read_object_statistic(1, duration)
                    if not shuffle_data_points_of_device_id == -1:
                        statistic.add_not_responding_device(device_id)
                        if logger.isEnabledFor(logging.INFO):
                            logger.info("Device pooling skipped: {}".format(device_id))
                        shuffle(data_points)
                        shuffle_data_points_of_device_id = -1
                    else:
                        statistic.remove_not_responding_device(device_id)
            except:
                self.logger.exception("Failed data collected")
            time.sleep(self.period)


# entry point of data_collector
if __name__ == '__main__':
    config.logging.initialize_logging()
    logger = logging.getLogger('visiobas.data_collector')
    argparser = argparse.ArgumentParser()
    argparser.add_argument("--device", type=int,
                           help="device identifier (request only specified device)")
    argparser.add_argument("--object", type=int,
                           help="object identifier (request only specified object worked with --device)")
    argparser.add_argument("--enable_verifier", type=int, default=1,
                           help="enable verifier module")
    argparser.add_argument("--enable_notifier", type=int, default=1,
                           help="enable notifier module")
    argparser.add_argument("--enable_transmitter", type=int, default=1,
                           help="enable transmitter module")
    argparser.add_argument("--read_app", type=str,
                           help="override read app")
    argparser.add_argument("--notify_fault", type=int, default=0, )
    argparser.add_argument("--single_thread", type=int, default=1,
                           help="using single thread instead of multi-threading data collect")
    argparser.add_argument("--shutdown", type=int, default=0,
                           help="shutdown timeout (sec)")
    argparser.add_argument("--write_put_requests", type=int, default=0,
                           help="write update data (put request) 1 - enable, 0 - disable")
    args = argparser.parse_args()

    address_cache_path = config.visiobas.address_cache_path
    if not Path(address_cache_path).is_file():
        logger.error("File 'address_cache' not found: {}".format(address_cache_path))
        exit(0)

    try:
        address_cache = Path(address_cache_path).read_text()
        address_cache_devices = BACnetParser.parse_bacwi(address_cache)
        if args.device is not None:
            address_cache_devices = list(filter(lambda x: x["id"] == args.device, address_cache_devices))
        device_ids = [x['id'] for x in address_cache_devices]

        # dict of all collecting bacnet objects
        # key - object id, value - BACnetObject
        # bacnet_objects = {}

        client = VisiobasGateClient(
            config.visiobas.visiobas_server['host'],
            config.visiobas.visiobas_server['port'],
            config.visiobas.visiobas_server['ssl_verify'],
            login=config.visiobas.visiobas_server['auth']['user'],
            md5_pwd=config.visiobas.visiobas_server['auth']['pwd'],
            write_put_requests=args.write_put_requests)

        try:
            # how often need to perform login ?
            client.rq_login(config.visiobas.visiobas_server['auth']['user'],
                            config.visiobas.visiobas_server['auth']['pwd'])

            # get notification class objects
            notification_class = client.rq_device_object(1, ObjectType.NOTIFICATION_CLASS)
            for o in notification_class:
                bacnet_network.append(NotificationClass(o))
                # bacnet_objects[o[ObjectProperty.OBJECT_IDENTIFIER.id()]] = NotificationClass(o)

            server_devices = client.rq_devices()
            server_devices = list(
                filter(lambda x: x[ObjectProperty.OBJECT_IDENTIFIER.id()] in device_ids, server_devices))
            if args.device is not None:
                server_devices = list(
                    filter(lambda x: x[ObjectProperty.OBJECT_IDENTIFIER.id()] == args.device, server_devices))
            for o in server_devices:
                device = Device(o)
                if args.read_app is not None:
                    device.set_read_app(args.read_app)
                bacnet_network.append(device)

            if not len(device_ids) == len(server_devices):
                logger.warning("Not all bacwi table devices exist on server")
                for address_cache_device in address_cache_devices:
                    device_id = address_cache_device['id']
                    found = next((x for x in server_devices
                                  if lambda d: d[ObjectProperty.OBJECT_IDENTIFIER.id()] == device_id), None)
                    if found is None:
                        logger.warning("Device not found on server side: {}".format(address_cache_device))

            port_devices = {}
            # group devices by port value
            # devices with different port value can be collected independently
            for address_cache_device in address_cache_devices:
                device_id = address_cache_device['id']
                server_device = bacnet_network.find_by_type(ObjectType.DEVICE, device_id)
                if not server_device:
                    # if _device_id not in bacnet_objects:
                    logger.warning("Device not found: {}".format(address_cache_device))
                    continue
                assert (type(server_device) == Device)

                host = address_cache_device['host']
                port = address_cache_device['port']

                # verify host and port equals on server device and bacwi device table
                if not host == server_device.get_host():
                    logger.warning("Server device {} host ({}) not equal with bacwi device host ({})".
                                   format(server_device.get_id(), server_device.get_host(), host))
                    logger.warning("Using bacwi host value for data collection.\n\
                                   Too resolve this issue update bacwi table or update server host value")
                    server_device.set_host(host)
                if not port == server_device.get_port():
                    logger.warning("Server device {} port ({}) not equal with bacwi device port ({})"
                                   .format(server_device.get_id(), server_device.get_port(), port))
                    logger.warning("Using bacwi port value for data collection.\n\
                                   Too resolve this issue update bacwi table or update server port value")
                    server_device.set_port(port)

                if args.single_thread == 1:
                    port = 47808

                if port not in port_devices:
                    port_devices[port] = []
                # assert type(server_device) == Device
                port_devices[port].append(server_device)

            thread_count = len(port_devices)

            transmitter = VisiobasTransmitter(client)
            transmitter.set_enable(not args.enable_transmitter == 0)
            transmitter.setDaemon(True)
            transmitter.start()

            notifier = VisiobasNotifier(client, bacnet_network)
            notifier.set_enable(not args.enable_notifier == 0)
            notifier.setDaemon(True)
            notifier.start()

            verifier = VisiobasDataVerifier(client, transmitter, notifier, bacnet_network)
            verifier.set_enable(not args.enable_verifier == 0)
            verifier.setDaemon(True)
            verifier.start()

            # list of object types for collect
            object_types = [
                ObjectType.ANALOG_INPUT,
                ObjectType.ANALOG_OUTPUT,
                ObjectType.ANALOG_VALUE,
                ObjectType.BINARY_INPUT,
                ObjectType.BINARY_OUTPUT,
                ObjectType.BINARY_VALUE,
                ObjectType.MULTI_STATE_INPUT,
                ObjectType.MULTI_STATE_OUTPUT,
                ObjectType.MULTI_STATE_VALUE
            ]

            collectors = []
            thread_idx = 1
            for port in port_devices:
                _devices = port_devices[port]
                data_collector_objects = []
                for device in _devices:
                    if logger.isEnabledFor(logging.INFO):
                        logger.info("Collecting data points for device: {}".format(device))
                    if device.get_read_app() is None:
                        logger.error("Device: {} read app not specified, ignore collecting data from device".format(
                            device.get_id()))
                        continue

                    for object_type in object_types:
                        objects = client.rq_device_object(device.get_id(), object_type)
                        if logger.isEnabledFor(logging.INFO):
                            object_ids = [x[ObjectProperty.OBJECT_IDENTIFIER.id()] for x in objects]
                            logger.info(
                                "Collector# {} device: {} type: {} objects: {}".format(thread_idx,
                                                                                       device.get_id(),
                                                                                       object_type,
                                                                                       object_ids))

                        if 'object' in args and args.object is not None:
                            objects = list(filter(
                                lambda x: x[ObjectProperty.OBJECT_IDENTIFIER.id()] == args.object, objects))

                        # collect map of bacnet object and link reference with notification class object
                        for o in objects:
                            bacnet_object = BACnetObject(o)
                            notification_class_id = bacnet_object.get_notification_class()
                            if not notification_class_id == 0:
                                # TODO time duration of find_by_type ?
                                notification_class = bacnet_network.find_by_type(ObjectType.NOTIFICATION_CLASS,
                                                                                 notification_class_id)
                                if notification_class:
                                    assert (type(notification_class) == NotificationClass)
                                    bacnet_object.set_notification_object(notification_class)
                            if args.notify_fault == 1:
                                notification_class = bacnet_object.get_notification_object()
                                if notification_class is None:
                                    notification_class = NotificationClass({
                                        ObjectProperty.DEVICE_ID.id(): 1,
                                        ObjectProperty.OBJECT_IDENTIFIER.id(): 55000,
                                        ObjectProperty.OBJECT_TYPE.id(): ObjectType.NOTIFICATION_CLASS.name(),
                                        ObjectProperty.OBJECT_PROPERTY_REFERENCE.id(): "Site:VisioBAS.Notification",
                                        ObjectProperty.DESCRIPTION.id(): "Default VisioBAS Notification",
                                        ObjectProperty.PRIORITY.id(): [2, 2, 2],
                                        ObjectProperty.RECIPIENT_LIST.id(): config.visiobas.notifier["recipient_list"]
                                    })
                                    bacnet_object.set_notification_object(notification_class)
                                else:
                                    recipient_list = notification_class.get_recipient_list()
                                    recipient_list += config.visiobas.notifier["recipient_list"]
                                    notification_class.set_recipient_list(recipient_list)
                                event_messages = config.visiobas.notifier["event_messages"]
                                for i in range(0, len(event_messages)):
                                    message = event_messages[i]
                                    if bacnet_object.get_event_message_text(i) == "":
                                        bacnet_object.set_event_message_text(i, message)
                                    bacnet_object.set_event_detection_enabled(True)
                                    bacnet_object.set_event_enable([True, True, True])
                            bacnet_network.append(bacnet_object)
                            data_collector_objects.append(bacnet_object)

                collector = VisiobasThreadDataCollector(thread_idx, verifier, bacnet_network)
                collector.setDaemon(True)
                if logger.isEnabledFor(logging.INFO):
                    _device_ids = [x.get_id() for x in _devices]
                    logger.info("Collector# {} collect devices: {}".format(thread_idx, _device_ids))
                for bacnet_object in data_collector_objects:
                    collector.add_object(bacnet_object)
                collector.start()
                collectors.append(collector)
                thread_idx += 1

            if logger.isEnabledFor(logging.INFO):
                if not os.path.exists("logs"):
                    os.mkdir("logs")
                bacnet_network.save("logs/bacnet_network.txt")

            # wait until all collector stop threads or shutdown on timeout
            # timeout = None if args.shutdown == 0 else args.shutdown
            app_start = time.time()
            max_heart_beat_timeout = 3600
            while True:
                time_stamp = time.time()
                if not args.shutdown == 0 and time_stamp - app_start > args.shutdown:
                    logger.error("Close app because of shutdown timeout")
                    exit(0)

                for collector in collectors:
                    if abs(time_stamp - collector.heart_beat) > max_heart_beat_timeout:
                        logger.error("Close app because of collector seems not alive any more")
                        exit(0)

                if args.enable_transmitter == 1:
                    if abs(time_stamp - transmitter.heart_beat) > max_heart_beat_timeout:
                        logger.error("Close app because of transmitter seems not alive any more")
                        exit(0)

                if args.enable_notifier == 1:
                    if abs(time_stamp - notifier.heart_beat) > max_heart_beat_timeout:
                        logger.error("Close app because of notifier seems not alive any more")
                        exit(0)

                if args.enable_verifier == 1:
                    if abs(time_stamp - verifier.heart_beat) > max_heart_beat_timeout:
                        logger.error("Close app because of verifier seems not alive any more")
                        exit(0)

                time.sleep(5)
        except BaseException as e:
            client.rq_logout()
            raise e
    except:
        logger.error(traceback.format_exc())
