# Copyright 2016 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Callable
from typing import List
from typing import Optional
from typing import Tuple
from typing import TypeVar

import weakref

from rcl_interfaces.msg import Parameter as ParameterMsg
from rcl_interfaces.msg import ParameterDescriptor
from rcl_interfaces.msg import ParameterEvent
from rcl_interfaces.msg import ParameterValue
from rcl_interfaces.msg import SetParametersResult
from rclpy.callback_groups import CallbackGroup
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.client import Client
from rclpy.clock import Clock
from rclpy.clock import ROSClock
from rclpy.constants import S_TO_NS
from rclpy.context import Context
from rclpy.exceptions import InvalidParameterValueException
from rclpy.exceptions import NotInitializedException
from rclpy.exceptions import ParameterAlreadyDeclaredException
from rclpy.exceptions import ParameterImmutableException
from rclpy.exceptions import ParameterNotDeclaredException
from rclpy.executors import Executor
from rclpy.expand_topic_name import expand_topic_name
from rclpy.guard_condition import GuardCondition
from rclpy.handle import Handle
from rclpy.impl.implementation_singleton import rclpy_implementation as _rclpy
from rclpy.logging import get_logger
from rclpy.parameter import Parameter
from rclpy.parameter_service import ParameterService
from rclpy.publisher import Publisher
from rclpy.qos import qos_profile_default
from rclpy.qos import qos_profile_parameter_events
from rclpy.qos import qos_profile_services_default
from rclpy.qos import QoSProfile
from rclpy.service import Service
from rclpy.subscription import Subscription
from rclpy.time_source import TimeSource
from rclpy.timer import WallTimer
from rclpy.type_support import check_for_type_support
from rclpy.utilities import get_default_context
from rclpy.validate_full_topic_name import validate_full_topic_name
from rclpy.validate_namespace import validate_namespace
from rclpy.validate_node_name import validate_node_name
from rclpy.validate_parameter_name import validate_parameter_name
from rclpy.validate_topic_name import validate_topic_name
from rclpy.waitable import Waitable

HIDDEN_NODE_PREFIX = '_'

# Used for documentation purposes only
MsgType = TypeVar('MsgType')
SrvType = TypeVar('SrvType')
SrvTypeRequest = TypeVar('SrvTypeRequest')
SrvTypeResponse = TypeVar('SrvTypeResponse')


class Node:
    """
    A Node in the ROS graph.

    A Node is the primary entrypoint in a ROS system for communication.
    It can be used to create ROS entities such as publishers, subscribers, services, etc.
    """

    def __init__(
        self,
        node_name: str,
        *,
        context: Context = None,
        cli_args: List[str] = None,
        namespace: str = None,
        use_global_arguments: bool = True,
        start_parameter_services: bool = True,
        initial_parameters: List[Parameter] = None,
        allow_undeclared_parameters: bool = False
    ) -> None:
        """
        Constructor.

        :param node_name: A name to give to this node. Validated by :func:`validate_node_name`.
        :param context: The context to be associated with, or ``None`` for the default global
            context.
        :param cli_args: A list of strings of command line args to be used only by this node.
        :param namespace: The namespace to which relative topic and service names will be prefixed.
            Validated by :func:`validate_namespace`.
        :param use_global_arguments: ``False`` if the node should ignore process-wide command line
            args.
        :param start_parameter_services: ``False`` if the node should not create parameter
            services.
        :param initial_parameters: A list of parameters to be set during node creation.
        :param allow_undeclared_parameters: True if undeclared parameters are allowed.
            This flag affects the behavior of parameter-related operations.
        """
        self._handle = None
        self._context = get_default_context() if context is None else context
        self._parameters: dict = {}
        self.publishers: List[Publisher] = []
        self.subscriptions: List[Subscription] = []
        self.clients: List[Client] = []
        self.services: List[Service] = []
        self.timers: List[WallTimer] = []
        self.guards: List[GuardCondition] = []
        self.waitables: List[Waitable] = []
        self._default_callback_group = MutuallyExclusiveCallbackGroup()
        self._parameters_callback = None
        self._allow_undeclared_parameters = allow_undeclared_parameters

        namespace = namespace or ''
        if not self._context.ok():
            raise NotInitializedException('cannot create node')
        try:
            self._handle = _rclpy.rclpy_create_node(
                node_name, namespace, self._context.handle, cli_args, use_global_arguments)
        except ValueError:
            # these will raise more specific errors if the name or namespace is bad
            validate_node_name(node_name)
            # emulate what rcl_node_init() does to accept '' and relative namespaces
            if not namespace:
                namespace = '/'
            if not namespace.startswith('/'):
                namespace = '/' + namespace
            validate_namespace(namespace)
            # Should not get to this point
            raise RuntimeError('rclpy_create_node failed for unknown reason')
        self._logger = get_logger(_rclpy.rclpy_get_node_logger_name(self.handle))

        # Clock that has support for ROS time.
        self._clock = ROSClock()
        self._time_source = TimeSource(node=self)
        self._time_source.attach_clock(self._clock)

        self.__executor_weakref = None

        self._parameter_event_publisher = self.create_publisher(
            ParameterEvent, 'parameter_events', qos_profile=qos_profile_parameter_events)

        node_parameters = _rclpy.rclpy_get_node_parameters(Parameter, self.handle)
        # Combine parameters from params files with those from the node constructor and
        # use the set_parameters_atomically API so a parameter event is published.
        if initial_parameters is not None:
            node_parameters.update({p.name: p for p in initial_parameters})

        self.declare_parameters('/', node_parameters.values())
        self.set_parameters_atomically(node_parameters.values())

        if start_parameter_services:
            self._parameter_service = ParameterService(self)

    @property
    def executor(self) -> Optional[Executor]:
        """Get the executor if the node has been added to one, else return ``None``."""
        if self.__executor_weakref:
            return self.__executor_weakref()
        return None

    @executor.setter
    def executor(self, new_executor: Executor) -> None:
        """Set or change the executor the node belongs to."""
        current_executor = self.executor
        if current_executor == new_executor:
            return
        if current_executor is not None:
            current_executor.remove_node(self)
        if new_executor is None:
            self.__executor_weakref = None
        else:
            new_executor.add_node(self)
            self.__executor_weakref = weakref.ref(new_executor)

    @property
    def context(self) -> Context:
        """Get the context associated with the node."""
        return self._context

    @property
    def default_callback_group(self) -> CallbackGroup:
        """
        Get the default callback group.

        If no other callback group is provided when the a ROS entity is created with the node,
        then it is added to the default callback group.
        """
        return self._default_callback_group

    @property
    def handle(self):
        """
        Get the handle to the underlying `rcl_node_t`.

        Cannot be modified after node creation.

        :raises: AttributeError if modified after creation.
        """
        return self._handle

    @handle.setter
    def handle(self, value):
        raise AttributeError('handle cannot be modified after node creation')

    def get_name(self) -> str:
        """Get the name of the node."""
        return _rclpy.rclpy_get_node_name(self.handle)

    def get_namespace(self) -> str:
        """Get the namespace of the node."""
        return _rclpy.rclpy_get_node_namespace(self.handle)

    def get_clock(self) -> Clock:
        """Get the clock used by the node."""
        return self._clock

    def get_logger(self):
        """Get the nodes logger."""
        return self._logger

    def declare_parameter(
        self,
        name: str,
        value: ParameterValue,
        descriptor: ParameterDescriptor
    ) -> Parameter:
        """
        Declare and initialize a parameter.

        This method, if successful, will result in any callback registered with
        :func:`set_parameters_callback` to be called.

        :param name: Name of the parameter to declare.
        :param value: Value of the parameter to declare.
        :param descriptor: Descriptor for the parameter to declare.
        :return: Parameter with the effectively assigned value.
        :raises: ParameterAlreadyDeclaredException if the parameter had already been declared.
        :raises: InvalidParameterException if the parameter name is invalid.
        :raises: InvalidParameterValueException if the registered callback rejects the parameter.
        """
        return self.declare_parameters('', [(name, value, descriptor)])[0]

    def declare_parameters(
        self,
        namespace: str,
        parameters: List[Tuple[str, ParameterValue, ParameterDescriptor]]
    ) -> List[Parameter]:
        """
        Declare a list of parameters.

        This method, if successful, will result in any callback registered with
        :func:`set_parameters_callback` to be called once for each parameter.
        If one of those calls fail, an exception will be raised and the remaining parameters will
        not be declared.
        Parameters declared up to that point will not be undeclared.

        :param namespace: Namespace for parameters.
        :param parameters: Tuple with parameters to declare, with a name, value and descriptor.
        :return: Parameter list with the effectively assigned values for each of them.
        :raises: ParameterAlreadyDeclaredException if the parameter had already been declared.
        :raises: InvalidParameterException if the parameter name is invalid.
        :raises: InvalidParameterValueException if the registered callback rejects any parameter.
        """
        parameter_list = []
        for parameter_tuple in parameters:
            name = parameter_tuple[0]
            assert isinstance(name, str)
            value = parameter_tuple[1]
            assert isinstance(value, ParameterValue)
            descriptor = parameter_tuple[2]
            assert isinstance(descriptor, ParameterDescriptor)

            # Note(jubeira): declare_parameters verifies the name, but set_parameters doesn't.
            full_name = namespace + name
            validate_parameter_name(full_name)

            parameter_list.append(Parameter.from_parameter_msg(
                ParameterMsg(name=full_name, value=value), descriptor))

        parameters_already_declared = (
            parameter.name in self._parameters for parameter in parameter_list
        )
        if any(parameters_already_declared):
            raise ParameterAlreadyDeclaredException(list(parameters_already_declared))

        # Call the callback once for each of the parameters, using method that doesn't
        # check whether the parameter was declared beforehand or not.
        self._set_parameters(parameter_list, raise_on_failure=True)
        return self.get_parameters([parameter.name for parameter in parameter_list])

    def undeclare_parameter(self, name: str):
        """
        Undeclare a previously declared parameter.

        This method will not cause a callback registered with
        :func:`set_parameters_callback` to be called.

        :param name: name of the parameter.
        :raises: ParameterNotDeclaredException if parameter had not been declared before.
        :raises: ParameterImmutableException if the parameter was created as read-only.
        """
        if self.has_parameter(name):
            if self._parameters[name].descriptor.read_only:
                raise ParameterImmutableException(name)
            else:
                del self._parameters[name]
        else:
            raise ParameterNotDeclaredException(name)

    def has_parameter(self, name: str) -> bool:
        """Return True if parameter is declared; False otherwise."""
        return name in self._parameters

    def get_parameters(self, names: List[str]) -> List[Parameter]:
        """
        Get a list of parameters.

        :param names: The names of the parameters to get.
        :return: The values for the given parameter names.
            A default Parameter will be returned for undeclared parameters if
            undeclared parameters are allowed.
        :raises: ParameterNotDeclaredException if undeclared parameters are not allowed,
            and at least one parameter hadn't been declared beforehand.
        """
        if not all(isinstance(name, str) for name in names):
            raise TypeError('All names must be instances of type str')
        return [self.get_parameter(name) for name in names]

    def get_parameter(self, name: str) -> Parameter:
        """
        Get a parameter by name.

        :param name: The name of the parameter.
        :return: The values for the given parameter names.
            A default Parameter will be returned for an undeclared parameter if
            undeclared parameters are allowed.
        :raises: ParameterNotDeclaredException if undeclared parameters are not allowed,
            and the parameter hadn't been declared beforehand.
        """
        if self.has_parameter(name):
            return self._parameters[name]
        elif self._allow_undeclared_parameters:
            return Parameter(name, Parameter.Type.NOT_SET, None)
        else:
            raise ParameterNotDeclaredException(name)

    def get_parameter_or(self, name: str, alternative_value: Parameter = None) -> Parameter:
        """
        Get a parameter or the alternative value.

        If the alternative value is None, a default Parameter with the given name and NOT_SET
        type will be returned.
        This method does not declare parameters in any case.

        :param name: Name of the parameter to get.
        :param alternative_value: Alternative parameter to get if it had not been declared before.
        :return: Requested parameter, or alternative value if it hadn't been declared before.
        """
        if alternative_value is None:
            alternative_value = Parameter(name, Parameter.Type.NOT_SET)

        return self._parameters.get(name, alternative_value)

    def set_parameters(self, parameter_list: List[Parameter]) -> List[SetParametersResult]:
        """
        Set parameters for the node, and return the result for the set action.

        If any parameter in the list was not declared beforehand and undeclared parameters are not
        allowed for the node, this method will raise a ParameterNotDeclaredException exception.

        Parameters are set in the order they are declared in the list.
        If setting a parameter fails due to not being declared, then the
        parameters which have already been set will stay set, and no attempt will
        be made to set the parameters which come after.

        If undeclared parameters are allowed, then all the parameters will be implicitly
        declared before being set even if they were not declared beforehand.

        If a callback was registered previously with :func:`set_parameters_callback`, it will be
        called prior to setting the parameters for the node, once for each parameter.
        If the callback prevents a parameter from being set, then it will be reflected in the
        returned result; no exceptions will be raised in this case.
        For each successfully set parameter, a :class:`ParameterEvent` message is
        published.

        If the value type of the parameter is NOT_SET, and the existing parameter type is
        something else, then the parameter will be implicitly undeclared.

        :param parameter_list: The list of parameters to set.
        :return: The result for each set action as a list.
        :raises: ParameterNotDeclaredException if undeclared parameters are not allowed,
            and at least one parameter in the list hadn't been declared beforehand.
        """
        return self._set_parameters(parameter_list, self.set_parameters_atomically)

    def _set_parameters(
        self,
        parameter_list: List[Parameter],
        setter_func=None,
        raise_on_failure=False
    ) -> List[SetParametersResult]:
        """
        Set parameters for the node, and return the result for the set action.

        Method for internal usage; applies a setter method for each parameters in the list.
        By default it doesn't check if the parameters were declared, and both declares and sets
        the given list.

        If a callback was registered previously with :func:`set_parameters_callback`, it will be
        called prior to setting the parameters for the node, once for each parameter.
        If the callback doesn't succeed for a given parameter, it won't be set and either an
        unsuccessful result will be returned for that parameter, or an exception will be raised
        according to `raise_on_failure` flag.

        :param parameter_list: List of parameters to set.
        :param setter_func: Method to set the parameters.
            Use :func:`_set_parameters_atomically` to declare and set the parameters without
            checking if they had been declared, or :func:`set_parameters_atomically`
            to check if they had been declared beforehand.
        :return: The result for each set action as a list.
        :raises: InvalidParameterValueException if the user-defined callback rejects the
            parameter value and raise_on_failure flag is True.
        """
        if setter_func is None:
            setter_func = self._set_parameters_atomically

        results = []
        for param in parameter_list:
            result = setter_func([param])
            if raise_on_failure and not result.successful:
                raise InvalidParameterValueException(param.name, param.value)
            results.append(result)
        return results

    def set_parameters_atomically(self, parameter_list: List[Parameter]) -> SetParametersResult:
        """
        Set the given parameters, all at one time, and then aggregate result.

        If any parameter in the list was not declared beforehand and undeclared parameters are not
        allowed for the node, this method will raise a ParameterNotDeclaredException exception.

        Parameters are set all at once.
        If setting a parameter fails due to not being declared, then no parameter will be set set.
        Either all of the parameters are set or none of them are set.

        If undeclared parameters are allowed, then all the parameters will be implicitly
        declared before being set even if they were not declared beforehand.

        If a callback was registered previously with :func:`set_parameters_callback`, it will be
        called prior to setting the parameters for the node only once for all parameters.
        If the callback prevents the parameters from being set, then it will be reflected in the
        returned result; no exceptions will be raised in this case.
        For each successfully set parameter, a :class:`ParameterEvent` message is published.

        If the value type of the parameter is NOT_SET, and the existing parameter type is
        something else, then the parameter will be implicitly undeclared.

        :param parameter_list: The list of parameters to set.
        :return: Aggregate result of setting all the parameters atomically.
        :raises: ParameterNotDeclaredException if undeclared parameters are not allowed,
            and at least one parameter in the list hadn't been declared beforehand.
        """
        if not all(isinstance(parameter, Parameter) for parameter in parameter_list):
            raise TypeError("parameter must be instance of type '{}'".format(repr(Parameter)))

        undeclared_parameters = (
            parameter.name not in self._parameters for parameter in parameter_list
        )
        if (not self._allow_undeclared_parameters and any(undeclared_parameters)):
            raise ParameterNotDeclaredException(list(undeclared_parameters))

        return self._set_parameters_atomically(parameter_list)

    def _set_parameters_atomically(self, parameter_list: List[Parameter]) -> SetParametersResult:
        """
        Set the given parameters, all at one time, and then aggregate result.

        This method does not check if the parameters were declared beforehand, and is intended
        for internal use of this class.

        If a callback was registered previously with :func:`set_parameters_callback`, it will be
        called prior to setting the parameters for the node only once for all parameters.
        If the callback prevents the parameters from being set, then it will be reflected in the
        returned result; no exceptions will be raised in this case.
        For each successfully set parameter, a :class:`ParameterEvent` message is
        published.

        If the value type of the parameter is NOT_SET, and the existing parameter type is
        something else, then the parameter will be implicitly undeclared.

        :param parameter_list: The list of parameters to set.
        :return: Aggregate result of setting all the parameters atomically.
        """
        result = None
        if self._parameters_callback:
            result = self._parameters_callback(parameter_list)
        else:
            result = SetParametersResult(successful=True)

        if result.successful:
            parameter_event = ParameterEvent()
            # Add fully qualified path of node to parameter event
            if self.get_namespace() == '/':
                parameter_event.node = self.get_namespace() + self.get_name()
            else:
                parameter_event.node = self.get_namespace() + '/' + self.get_name()
            for param in parameter_list:
                if Parameter.Type.NOT_SET == param.type_:
                    if Parameter.Type.NOT_SET != self.get_parameter_or(param.name).type_:
                        # Parameter deleted. (Parameter had value and new value is not set)
                        parameter_event.deleted_parameters.append(
                            param.to_parameter_msg())
                    # Delete any unset parameters regardless of their previous value.
                    # We don't currently store NOT_SET parameters so this is an extra precaution.
                    if param.name in self._parameters:
                        del self._parameters[param.name]
                else:
                    if Parameter.Type.NOT_SET == self.get_parameter_or(param.name).type_:
                        #  Parameter is new. (Parameter had no value and new value is set)
                        parameter_event.new_parameters.append(param.to_parameter_msg())
                    else:
                        # Parameter changed. (Parameter had a value and new value is set)
                        parameter_event.changed_parameters.append(
                            param.to_parameter_msg())
                    self._parameters[param.name] = param
            parameter_event.stamp = self._clock.now().to_msg()
            self._parameter_event_publisher.publish(parameter_event)

        return result

    def describe_parameter(self, name: str) -> ParameterDescriptor:
        """
        Get the parameter descriptor of a given parameter.

        :param name: Name of the parameter to describe.
        :return: ParameterDescriptor corresponding to the parameter,
            or default ParameterDescriptor if parameter had not been declared before
            and undeclared parameters are allowed.
        :raises: ParameterNotDeclaredException if parameter had not been declared before
            and undeclared parameters are not allowed.
        """
        try:
            return self._parameters[name].descriptor
        except KeyError:
            if self._allow_undeclared_parameters:
                return ParameterDescriptor()
            else:
                raise ParameterNotDeclaredException(name)

    def describe_parameters(self, names: List[str]) -> List[ParameterDescriptor]:
        """
        Get the parameter descriptors of a given list of parameters.

        :param name: List of names of the parameters to describe.
        :return: List of ParameterDescriptors corresponding to the given parameters.
            Default ParameterDescriptors shall be returned for parameters that
            had not been declared before if undeclared parameters are allowed.
        :raises: ParameterNotDeclaredException if at least one parameter
            had not been declared before and undeclared parameters are not allowed.
        """
        parameter_descriptors = []
        for name in names:
            parameter_descriptors.append(self.describe_parameter(name))

        return parameter_descriptors

    def set_parameters_callback(
        self,
        callback: Callable[[List[Parameter]], SetParametersResult]
    ) -> None:
        """
        Register a set parameters callback.

        Calling this function with override any previously registered callback.

        :param callback: The function that is called whenever parameters are set for the node.
        """
        self._parameters_callback = callback

    def _validate_topic_or_service_name(self, topic_or_service_name, *, is_service=False):
        name = self.get_name()
        namespace = self.get_namespace()
        validate_node_name(name)
        validate_namespace(namespace)
        validate_topic_name(topic_or_service_name, is_service=is_service)
        expanded_topic_or_service_name = expand_topic_name(topic_or_service_name, name, namespace)
        validate_full_topic_name(expanded_topic_or_service_name, is_service=is_service)

    def add_waitable(self, waitable: Waitable) -> None:
        """
        Add a class that is capable of adding things to the wait set.

        :param waitable: An instance of a waitable that the node will add to the waitset.
        """
        self.waitables.append(waitable)

    def remove_waitable(self, waitable: Waitable) -> None:
        """
        Remove a Waitable that was previously added to the node.

        :param waitable: The Waitable to remove.
        """
        self.waitables.remove(waitable)

    def create_publisher(
        self,
        msg_type,
        topic: str,
        *,
        qos_profile: QoSProfile = qos_profile_default
    ) -> Publisher:
        """
        Create a new publisher.

        :param msg_type: The type of ROS messages the publisher will publish.
        :param topic: The name of the topic the publisher will publish to.
        :param qos_profile: The quality of service profile to apply to the publisher.
        :return: The new publisher.
        """
        # this line imports the typesupport for the message module if not already done
        check_for_type_support(msg_type)
        failed = False
        try:
            publisher_handle = _rclpy.rclpy_create_publisher(
                self.handle, msg_type, topic, qos_profile.get_c_qos_profile())
        except ValueError:
            failed = True
        if failed:
            self._validate_topic_or_service_name(topic)
        publisher = Publisher(publisher_handle, msg_type, topic, qos_profile, self.handle)
        self.publishers.append(publisher)
        return publisher

    def create_subscription(
        self,
        msg_type,
        topic: str,
        callback: Callable[[MsgType], None],
        *,
        qos_profile: QoSProfile = qos_profile_default,
        callback_group: CallbackGroup = None,
        raw: bool = False
    ) -> Subscription:
        """
        Create a new subscription.

        :param msg_type: The type of ROS messages the subscription will subscribe to.
        :param topic: The name of the topic the subscription will subscribe to.
        :param callback: A user-defined callback function that is called when a message is
            received by the subscription.
        :param qos_profile: The quality of service profile to apply to the subscription.
        :param callback_group: The callback group for the subscription. If ``None``, then the
            nodes default callback group is used.
        :param raw: If ``True``, then received messages will be stored in raw binary
            representation.
        """
        if callback_group is None:
            callback_group = self.default_callback_group
        # this line imports the typesupport for the message module if not already done
        check_for_type_support(msg_type)
        failed = False
        try:
            subscription_capsule = _rclpy.rclpy_create_subscription(
                self.handle, msg_type, topic, qos_profile.get_c_qos_profile())
        except ValueError:
            failed = True
        if failed:
            self._validate_topic_or_service_name(topic)

        subscription_handle = Handle(subscription_capsule)

        subscription = Subscription(
            subscription_handle, msg_type,
            topic, callback, callback_group, qos_profile, self.handle, raw)
        self.subscriptions.append(subscription)
        callback_group.add_entity(subscription)
        return subscription

    def create_client(
        self,
        srv_type,
        srv_name: str,
        *,
        qos_profile: QoSProfile = qos_profile_services_default,
        callback_group: CallbackGroup = None
    ) -> Client:
        """
        Create a new service client.

        :param srv_type: The service type.
        :param srv_name: The name of the service.
        :param qos_profile: The quality of service profile to apply the service client.
        :param callback_group: The callback group for the service client. If ``None``, then the
            nodes default callback group is used.
        """
        if callback_group is None:
            callback_group = self.default_callback_group
        check_for_type_support(srv_type)
        failed = False
        try:
            [client_handle, client_pointer] = _rclpy.rclpy_create_client(
                self.handle,
                srv_type,
                srv_name,
                qos_profile.get_c_qos_profile())
        except ValueError:
            failed = True
        if failed:
            self._validate_topic_or_service_name(srv_name, is_service=True)
        client = Client(
            self.handle, self.context,
            client_handle, client_pointer, srv_type, srv_name, qos_profile,
            callback_group)
        self.clients.append(client)
        callback_group.add_entity(client)
        return client

    def create_service(
        self,
        srv_type,
        srv_name: str,
        callback: Callable[[SrvTypeRequest, SrvTypeResponse], SrvTypeResponse],
        *,
        qos_profile: QoSProfile = qos_profile_services_default,
        callback_group: CallbackGroup = None
    ) -> Service:
        """
        Create a new service server.

        :param srv_type: The service type.
        :param srv_name: The name of the service.
        :param callback: A user-defined callback function that is called when a service request
            received by the server.
        :param qos_profile: The quality of service profile to apply the service server.
        :param callback_group: The callback group for the service server. If ``None``, then the
            nodes default callback group is used.
        """
        if callback_group is None:
            callback_group = self.default_callback_group
        check_for_type_support(srv_type)
        failed = False
        try:
            [service_handle, service_pointer] = _rclpy.rclpy_create_service(
                self.handle,
                srv_type,
                srv_name,
                qos_profile.get_c_qos_profile())
        except ValueError:
            failed = True
        if failed:
            self._validate_topic_or_service_name(srv_name, is_service=True)
        service = Service(
            self.handle, service_handle, service_pointer,
            srv_type, srv_name, callback, callback_group, qos_profile)
        self.services.append(service)
        callback_group.add_entity(service)
        return service

    def create_timer(
        self,
        timer_period_sec: float,
        callback: Callable,
        callback_group: CallbackGroup = None
    ) -> WallTimer:
        """
        Create a new timer.

        The timer will be started and every ``timer_period_sec`` number of seconds the provided
        callback function will be called.

        :param timer_period_sec: The period (s) of the timer.
        :param callback: A user-defined callback function that is called when the timer expires.
        :param callback_group: The callback group for the timer. If ``None``, then the nodes
            default callback group is used.
        """
        timer_period_nsec = int(float(timer_period_sec) * S_TO_NS)
        if callback_group is None:
            callback_group = self.default_callback_group
        timer = WallTimer(callback, callback_group, timer_period_nsec, context=self.context)

        self.timers.append(timer)
        callback_group.add_entity(timer)
        return timer

    def create_guard_condition(
        self,
        callback: Callable,
        callback_group: CallbackGroup = None
    ) -> GuardCondition:
        """Create a new guard condition."""
        if callback_group is None:
            callback_group = self.default_callback_group
        guard = GuardCondition(callback, callback_group, context=self.context)

        self.guards.append(guard)
        callback_group.add_entity(guard)
        return guard

    def destroy_publisher(self, publisher: Publisher) -> bool:
        """
        Destroy a publisher created by the node.

        :return: ``True`` if successful, ``False`` otherwise.
        """
        for pub in self.publishers:
            if pub.publisher_handle == publisher.publisher_handle:
                _rclpy.rclpy_destroy_node_entity(pub.publisher_handle, self.handle)
                self.publishers.remove(pub)
                return True
        return False

    def destroy_subscription(self, subscription: Subscription) -> bool:
        """
        Destroy a subscription created by the node.

        :return: ``True`` if succesful, ``False`` otherwise.
        """
        if subscription in self.subscriptions:
            self.subscriptions.remove(subscription)
            subscription.destroy()
            return True
        return False

    def destroy_client(self, client: Client) -> bool:
        """
        Destroy a service client created by the node.

        :return: ``True`` if successful, ``False`` otherwise.
        """
        for cli in self.clients:
            if cli.client_handle == client.client_handle:
                _rclpy.rclpy_destroy_node_entity(cli.client_handle, self.handle)
                self.clients.remove(cli)
                return True
        return False

    def destroy_service(self, service: Service) -> bool:
        """
        Destroy a service server created by the node.

        :return: ``True`` if successful, ``False`` otherwise.
        """
        for srv in self.services:
            if srv.service_handle == service.service_handle:
                _rclpy.rclpy_destroy_node_entity(srv.service_handle, self.handle)
                self.services.remove(srv)
                return True
        return False

    def destroy_timer(self, timer: WallTimer) -> bool:
        """
        Destroy a timer created by the node.

        :return: ``True`` if successful, ``False`` otherwise.
        """
        for tmr in self.timers:
            if tmr.timer_handle == timer.timer_handle:
                _rclpy.rclpy_destroy_entity(tmr.timer_handle)
                # TODO(sloretz) Store clocks on node and destroy them separately
                _rclpy.rclpy_destroy_entity(tmr.clock._clock_handle)
                self.timers.remove(tmr)
                return True
        return False

    def destroy_guard_condition(self, guard: GuardCondition) -> bool:
        """
        Destroy a guard condition created by the node.

        :return: ``True`` if successful, ``False`` otherwise.
        """
        for gc in self.guards:
            if gc.guard_handle == guard.guard_handle:
                _rclpy.rclpy_destroy_entity(gc.guard_handle)
                self.guards.remove(gc)
                return True
        return False

    def destroy_node(self) -> bool:
        """
        Destroy the node.

        Frees resources used by the node, including any entities created by the following methods:

        * :func:`create_publisher`
        * :func:`create_subscription`
        * :func:`create_client`
        * :func:`create_service`
        * :func:`create_timer`
        * :func:`create_guard_condition`

        :return: ``True`` if successful, ``False`` otherwise.
        """
        ret = True
        if self.handle is None:
            return ret

        # Drop extra reference to parameter event publisher.
        # It will be destroyed with other publishers below.
        self._parameter_event_publisher = None

        while self.publishers:
            pub = self.publishers.pop()
            _rclpy.rclpy_destroy_node_entity(pub.publisher_handle, self.handle)
        while self.subscriptions:
            sub = self.subscriptions.pop()
            sub.destroy()
        while self.clients:
            cli = self.clients.pop()
            _rclpy.rclpy_destroy_node_entity(cli.client_handle, self.handle)
        while self.services:
            srv = self.services.pop()
            _rclpy.rclpy_destroy_node_entity(srv.service_handle, self.handle)
        while self.timers:
            tmr = self.timers.pop()
            _rclpy.rclpy_destroy_entity(tmr.timer_handle)
            # TODO(sloretz) Store clocks on node and destroy them separately
            _rclpy.rclpy_destroy_entity(tmr.clock._clock_handle)
        while self.guards:
            gc = self.guards.pop()
            _rclpy.rclpy_destroy_entity(gc.guard_handle)

        _rclpy.rclpy_destroy_entity(self.handle)
        self._handle = None
        return ret

    def get_publisher_names_and_types_by_node(
        self,
        node_name: str,
        node_namespace: str,
        no_demangle: bool = False
    ) -> List[Tuple[str, str]]:
        """
        Get a list of discovered topics for publishers of a remote node.

        :param node_name: Name of a remote node to get publishers for.
        :param node_namespace: Namespace of the remote node.
        :param no_demangle: If ``True``, then topic names and types returned will not be demangled.
        :return: List of tuples containing two strings: the topic name and topic type.
        """
        return _rclpy.rclpy_get_publisher_names_and_types_by_node(
            self.handle, no_demangle, node_name, node_namespace)

    def get_subscriber_names_and_types_by_node(
        self,
        node_name: str,
        node_namespace: str,
        no_demangle: bool = False
    ) -> List[Tuple[str, str]]:
        """
        Get a list of discovered topics for subscriptions of a remote node.

        :param node_name: Name of a remote node to get subscriptions for.
        :param node_namespace: Namespace of the remote node.
        :param no_demangle: If ``True``, then topic names and types returned will not be demangled.
        :return: List of tuples containing two strings: the topic name and topic type.
        """
        return _rclpy.rclpy_get_subscriber_names_and_types_by_node(
            self.handle, no_demangle, node_name, node_namespace)

    def get_service_names_and_types_by_node(
        self,
        node_name: str,
        node_namespace: str
    ) -> List[Tuple[str, str]]:
        """
        Get a list of discovered service topics for a remote node.

        :param node_name: Name of a remote node to get services for.
        :param node_namespace: Namespace of the remote node.
        :return: List of tuples containing two strings: the service name and service type.
        """
        return _rclpy.rclpy_get_service_names_and_types_by_node(
            self.handle, node_name, node_namespace)

    def get_topic_names_and_types(self, no_demangle: bool = False) -> List[Tuple[str, str]]:
        """
        Get a list topic names and types for the node.

        :param no_demangle: If ``True``, then topic names and types returned will not be demangled.
        :return: List of tuples containing two strings: the topic name and topic type.
        """
        return _rclpy.rclpy_get_topic_names_and_types(self.handle, no_demangle)

    def get_service_names_and_types(self) -> List[Tuple[str, str]]:
        """
        Get a list of service topics for the node.

        :return: List of tuples containing two strings: the service name and service type.
        """
        return _rclpy.rclpy_get_service_names_and_types(self.handle)

    def get_node_names(self) -> List[str]:
        """
        Get a list of names for discovered nodes.

        :return: List of node names.
        """
        names_ns = _rclpy.rclpy_get_node_names_and_namespaces(self.handle)
        return [n[0] for n in names_ns]

    def get_node_names_and_namespaces(self) -> List[Tuple[str, str]]:
        """
        Get a list of names and namespaces for discovered nodes.

        :return: List of tuples containing two strings: the node name and node namespace.
        """
        return _rclpy.rclpy_get_node_names_and_namespaces(self.handle)

    def _count_publishers_or_subscribers(self, topic_name, func):
        fq_topic_name = expand_topic_name(topic_name, self.get_name(), self.get_namespace())
        validate_topic_name(fq_topic_name)
        return func(self.handle, fq_topic_name)

    def count_publishers(self, topic_name: str) -> int:
        """
        Return the number of publishers on a given topic.

        `topic_name` may be a relative, private, or fully qualifed topic name.
        A relative or private topic is expanded using this node's namespace and name.
        The queried topic name is not remapped.

        :param topic_name: the topic_name on which to count the number of publishers.
        :return: the number of publishers on the topic.
        """
        return self._count_publishers_or_subscribers(topic_name, _rclpy.rclpy_count_publishers)

    def count_subscribers(self, topic_name: str) -> int:
        """
        Return the number of subscribers on a given topic.

        `topic_name` may be a relative, private, or fully qualifed topic name.
        A relative or private topic is expanded using this node's namespace and name.
        The queried topic name is not remapped.

        :param topic_name: the topic_name on which to count the number of subscribers.
        :return: the number of subscribers on the topic.
        """
        return self._count_publishers_or_subscribers(topic_name, _rclpy.rclpy_count_subscribers)

    def __del__(self):
        self.destroy_node()
