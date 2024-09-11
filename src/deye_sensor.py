# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import math
from datetime import datetime
from abc import abstractmethod, abstractproperty


class Sensor:
    """
    Models solar inverter sensor.

    This is an abstract class. Method 'read_value' must be provided by the extending subclass.
    """

    @abstractproperty
    def mqtt_topic_suffix(self) -> str:
        pass

    @abstractmethod
    def read_value(self, registers: dict[int, bytearray]):
        """
        Reads sensor value from Modbus registers
        """
        pass

    def write_value(self, value: str) -> dict[int, bytearray]:
        """
        Converts value into bytes representation.
        Useful for configuration modifications, when values are written to the inverter.
        """
        return {}

    def format_value(self, value):
        """
        Formats sensor value using configured format string
        """
        return self.print_format.format(value)

    def in_any_group(self, active_groups: set[str]) -> bool:
        """
        Checks if this sensor is included in at least one of the given active_groups.
        Sensor matches any group when its groups set is empty (default behavior)
        """
        return not self.groups or len(active_groups.intersection(self.groups)) > 0

    @abstractmethod
    def get_registers(self) -> list[int]:
        """Returns the list of Modbus registers read by this sensor"""


class DailyResetSensor(Sensor):
    """
    Wraps other sensor and ensures that the value reported is reset daily.
    This is useful to avoid the last value measured yesterday being reported as the first value of today.
    Implemented to mitigate microinverter daily energy value "leak".
    """

    def __init__(self, delegate: Sensor):
        self.__delegate = delegate
        self.__last_value = 0
        self.__last_value_ts = datetime.now()

    @property
    def mqtt_topic_suffix(self) -> str:
        return self.__delegate.mqtt_topic_suffix

    def read_value(self, registers: dict[int, bytearray]):
        now = datetime.now()
        value = self.__delegate.read_value(registers)
        if value is not None and now.day != self.__last_value_ts.day and value >= self.__last_value:
            return 0
        self.__last_value = value
        self.__last_value_ts = now
        return value

    def write_value(self, value: str) -> dict[int, bytearray]:
        return self.__delegate.write_value(value)

    def format_value(self, value):
        return self.__delegate.format_value(value)

    def in_any_group(self, active_groups: set[str]) -> bool:
        return self.__delegate.in_any_group(active_groups)

    def get_registers(self) -> list[int]:
        return self.__delegate.get_registers()


class NamedSensor(Sensor):
    def __init__(self, name: str, mqtt_topic_suffix="", unit="", print_format="{:s}", groups=[]):
        self.name = name
        self.__mqtt_topic_suffix = mqtt_topic_suffix
        self.unit = unit
        self.print_format = print_format
        assert len(groups) > 0, f"Sensor {name} must belong to at least one group"
        self.groups = groups

    @property
    def mqtt_topic_suffix(self) -> str:
        return self.__mqtt_topic_suffix


class SingleRegisterSensor(NamedSensor):
    """
    Solar inverter sensor with value stored as 32-bit integer in a single Modbus register.
    """

    def __init__(
        self,
        name: str,
        reg_address: int,
        factor: float,
        offset: float = 0,
        signed=False,
        mqtt_topic_suffix="",
        unit="",
        print_format="{:0.1f}",
        groups=[],
    ):
        super().__init__(name, mqtt_topic_suffix, unit, print_format, groups)
        self.reg_address = reg_address
        self.factor = factor
        self.offset = offset
        self.signed = signed

    def read_value(self, registers: dict[int, bytearray]):
        if self.reg_address in registers:
            reg_value = registers[self.reg_address]
            return int.from_bytes(reg_value, "big", signed=self.signed) * self.factor + self.offset
        else:
            return None

    def write_value(self, value: str) -> dict[int, bytearray]:
        v = int((float(value) - self.offset) / self.factor)
        return {self.reg_address: v.to_bytes(2, "big", signed=self.signed)}

    @abstractmethod
    def get_registers(self) -> list[int]:
        return [self.reg_address]

    def reset_daily(self) -> DailyResetSensor:
        return DailyResetSensor(self)


class DoubleRegisterSensor(NamedSensor):
    """
    Solar inverter sensor with value stored as 64-bit integer in two Modbus registers.
    """

    def __init__(
        self,
        name: str,
        reg_address: int,
        factor: float,
        offset: float = 0,
        signed=False,
        mqtt_topic_suffix="",
        unit="",
        print_format="{:0.1f}",
        groups=[],
        low_word_first=True,
    ):
        super().__init__(name, mqtt_topic_suffix, unit, print_format, groups)
        self.reg_address = reg_address
        self.factor = factor
        self.offset = offset
        self.signed = signed
        self.low_word_first = low_word_first

    def read_value(self, registers: dict[int, bytearray]):
        low_word_reg_address = self.reg_address + (0 if self.low_word_first else 1)
        high_word_reg_address = self.reg_address + (1 if self.low_word_first else 0)
        if low_word_reg_address in registers and high_word_reg_address in registers:
            low_word = registers[low_word_reg_address]
            high_word = registers[high_word_reg_address]
            return int.from_bytes(high_word + low_word, "big", signed=self.signed) * self.factor + self.offset
        else:
            return None

    @abstractmethod
    def get_registers(self) -> list[int]:
        return [self.reg_address, self.reg_address + 1]

    def reset_daily(self) -> DailyResetSensor:
        return DailyResetSensor(self)


class SignedMagnitudeSingleRegisterSensor(SingleRegisterSensor):
    """
    Reads single Modbus register as signed, fixed point 15-bits long value.
    The most significant bit encodes the sign.
    """

    def read_value(self, registers: dict[int, bytearray]):
        if self.reg_address in registers:
            reg_value = int.from_bytes(registers[self.reg_address], "big", signed=False) & 0x7FFF
            # If highest bit is set, we've got a negative value
            if bool(registers[self.reg_address][0] & 0x80):
                return -1 * reg_value * self.factor + self.offset
            else:
                return reg_value * self.factor + self.offset
        else:
            return None


class SignedMagnitudeDoubleRegisterSensor(DoubleRegisterSensor):
    """
    Reads double Modbus registers as signed, fixed point 31-bits long value.
    The most significant bit encodes the sign.
    """

    def read_value(self, registers: dict[int, bytearray]):
        high_word_reg_address = self.reg_address
        low_word_reg_address = self.reg_address + 1
        if low_word_reg_address in registers and high_word_reg_address in registers:
            low_word = registers[low_word_reg_address]
            high_word = registers[high_word_reg_address]
            reg_value = int.from_bytes(high_word + low_word, "big", signed=False) & 0x7FFFFFFF
            # If highest bit is set, we've got a negative value
            if bool(registers[self.reg_address][0] & 0x80):
                return -1 * reg_value * self.factor + self.offset
            else:
                return reg_value * self.factor + self.offset
        else:
            return None


class ComputedPowerSensor(NamedSensor):
    """
    Electric Power sensor with value computed as multiplication of values read by voltage and current sensors.
    """

    def __init__(
        self,
        name: str,
        voltage_sensor: Sensor,
        current_sensor: Sensor,
        mqtt_topic_suffix="",
        unit="",
        print_format="{:0.1f}",
        groups=[],
    ):
        super().__init__(name, mqtt_topic_suffix, unit, print_format, groups)
        self.voltage_sensor = voltage_sensor
        self.current_sensor = current_sensor

    def read_value(self, registers: dict[int, bytearray]):
        voltage = self.voltage_sensor.read_value(registers)
        current = self.current_sensor.read_value(registers)
        if voltage is not None and current is not None:
            return voltage * current
        else:
            return None

    def get_registers(self) -> list[int]:
        return []


class ComputedSumSensor(NamedSensor):
    """
    Computes a sum of values read by given list of sensors.
    """

    def __init__(
        self, name: str, sensors: list[Sensor], mqtt_topic_suffix="", unit="", print_format="{:0.1f}", groups=[]
    ):
        super().__init__(name, mqtt_topic_suffix, unit, print_format, groups)
        self.sensors = sensors

    def read_value(self, registers: dict[int, bytearray]):
        result = 0
        sensor_values = [s.read_value(registers) for s in self.sensors]
        for value in sensor_values:
            if value is None:
                return None
            result += value
        return result

    def get_registers(self) -> list[int]:
        return []


class AggregatedValueSensor(NamedSensor):
    """
    Represents value computed as an aggregation in multi-inverter installation
    """

    def __init__(self, name: str, mqtt_topic_suffix="", unit="", print_format="{:0.1f}", groups=[]):
        super().__init__(name, mqtt_topic_suffix, unit, print_format, groups)

    def read_value(self, registers: dict[int, bytearray]):
        raise RuntimeError("Cannot read registers of aggregated sensor")

    def write_value(self, value: str) -> dict[int, bytearray]:
        raise RuntimeError("Cannot write registers of aggregated sensor")

    def get_registers(self) -> list[int]:
        return []


class SensorRegisterRange:
    """
    Declares a Modbus register range that must be read to provide values for sensors within a metrics group
    """

    def __init__(self, group: str | set[str], first_reg_address: int, last_reg_address: int):
        self.group = group if isinstance(group, set) else {group}
        self.first_reg_address = first_reg_address
        self.last_reg_address = last_reg_address

    def in_any_group(self, active_groups: set[str]) -> bool:
        """
        Checks if this range is included in at least one of the given active_groups.
        """
        return len(self.group.intersection(active_groups)) > 0

    def is_same_range(self, other: "SensorRegisterRange") -> bool:
        """Checks if the other range has this same first and last reg address.

        Args:
            other (SensorRegisterRange): to check against

        Returns:
            bool: True when both ranges define this same registers addresses, False otherwise
        """
        return self.first_reg_address == other.first_reg_address and self.last_reg_address == other.last_reg_address

    @property
    def length(self) -> int:
        return self.last_reg_address - self.first_reg_address + 1

    def split(self, sub_range_len: int) -> list["SensorRegisterRange"]:
        """Splits this register range into sub-ranges

        Args:
            sub_range_len (int): only ranges longer than this value are splitted

        Returns:
            list[SensorRegisterRange]: created sub-ranges
        """
        sub_ranges: list[SensorRegisterRange] = []
        sub_ranges_count = math.ceil(self.length / sub_range_len)
        for i in range(0, sub_ranges_count):
            sub_range_first_reg = self.first_reg_address + i * sub_range_len
            sub_range_last_reg = min(sub_range_first_reg + sub_range_len - 1, self.last_reg_address)
            sub_ranges.append(SensorRegisterRange(self.group, sub_range_first_reg, sub_range_last_reg))
        return sub_ranges

    def __str__(self):
        return "metrics group: {}, range: {:04x}-{:04x}".format(
            self.group, self.first_reg_address, self.last_reg_address
        )


class SensorRegisterRanges:
    def __init__(self, ranges: list[SensorRegisterRange], metric_groups: list[str], max_range_length: int):
        filtered_ranges = SensorRegisterRanges.__filter_reg_ranges(ranges, metric_groups)
        unique_ranges = SensorRegisterRanges.__remove_duplicated_reg_ranges(filtered_ranges)
        self.ranges = SensorRegisterRanges.__split_long_reg_ranges(unique_ranges, max_range_length)

    @staticmethod
    def __filter_reg_ranges(
        reg_ranges_to_filter: list[SensorRegisterRange], metric_groups: list[str]
    ) -> list[SensorRegisterRange]:
        return [r for r in reg_ranges_to_filter if r.in_any_group(metric_groups)]

    @staticmethod
    def __split_long_reg_ranges(
        reg_ranges: list[SensorRegisterRange], max_range_length: int
    ) -> list[SensorRegisterRange]:
        result: list[SensorRegisterRange] = []
        for reg_range in reg_ranges:
            if reg_range.length <= max_range_length:
                result.append(reg_range)
            else:
                result += reg_range.split(max_range_length)
        return result

    @staticmethod
    def __remove_duplicated_reg_ranges(reg_ranges: list[SensorRegisterRange]) -> list[SensorRegisterRange]:
        result: list[SensorRegisterRange] = []
        for reg_range in reg_ranges:
            if not [r for r in result if r.is_same_range(reg_range)]:
                result.append(reg_range)
        return result
