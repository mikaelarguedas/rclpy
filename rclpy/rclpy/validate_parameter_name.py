# Copyright 2019 Open Source Robotics Foundation, Inc.
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

from rclpy.exceptions import InvalidParameterException
from rclpy.impl.implementation_singleton import rclpy_implementation as _rclpy


def validate_parameter_name(name):
    """
    Validate a given parameter name, and raise an exception if invalid.

    The name does not have to be fully-qualified and is not expanded.

    If the name is invalid then rclpy.exceptions.InvalidParameterNameException
    will be raised.

    :param name str: topic or service name to be validated
    :param is_service bool: if true, InvalidServiceNameException is raised
    :returns: True when it is valid
    :raises: InvalidParameterNameException: when the name is invalid
    """
    # TODO(jubeira): define whether this is an appropriate method to validate
    # parameter names.
    result = _rclpy.rclpy_get_validation_error_for_topic_name(name)
    if result is None:
        return True
    error_msg, invalid_index = result
    raise InvalidParameterException(name, error_msg, invalid_index)