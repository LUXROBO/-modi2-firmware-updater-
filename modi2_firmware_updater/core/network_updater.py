import json
import sys
import threading as th
import time
from io import open
from os import path

from serial.serialutil import SerialException

from modi2_firmware_updater.util.message_util import parse_message, unpack_data
from modi2_firmware_updater.util.modi_winusb.modi_serialport import ModiSerialPort, list_modi_serialports
from modi2_firmware_updater.util.module_util import Module, get_module_type_from_uuid
from modi2_firmware_updater.util.platform_util import delay


class NetworkFirmwareUpdater(ModiSerialPort):
    """Network Firmware Updater: Updates a firmware of given module"""

    NO_ERROR = 0
    UPDATE_READY = 1
    WRITE_FAIL = 2
    VERIFY_FAIL = 3
    CRC_ERROR = 4
    CRC_COMPLETE = 5
    ERASE_ERROR = 6
    ERASE_COMPLETE = 7

    def __init__(self, device=None, module_firmware_path=None):
        self.print = True
        if device is not None:
            super().__init__(device, baudrate=921600, timeout=0.1, write_timeout=0)
        else:
            modi_ports = list_modi_serialports()
            if not modi_ports:
                raise SerialException("No MODI+ port is connected")
            for modi_port in modi_ports:
                try:
                    super().__init__(modi_port, baudrate=921600, timeout=0.1, write_timeout=0)
                except Exception:
                    self.__print('Next network module')
                    continue
                else:
                    break
            self.__print(f"Connecting to MODI+ network module at {modi_port}")

        self.bootloader = False
        self.is_network = True
        self.network_version = None
        self.network_uuid = None
        self.network_id = None

        self.update_in_progress = False

        self.progress = 0

        self.popup_reconnect = False
        self.raise_error_message = True
        self.update_error = 0
        self.update_error_message = ""
        self.has_update_error = False

        self.module_firmware_path = module_firmware_path

    def set_print(self, print):
        self.print = print

    def set_raise_error(self, raise_error_message):
        self.raise_error_message = raise_error_message

    def get_connected_module_info(self):
        timeout = 3
        init_time = time.time()
        while True:
            self.__print("request uuid")
            self.send_request_network_uuid()
            self.__print("wait for request")
            recved = self.wait_for_json(timeout)

            if time.time() - init_time > timeout:
                return None, None, None

            try:
                if not recved:
                    continue

                json_msg = json.loads(recved)
                if json_msg["c"] == 0x05:
                    unpacked_data = unpack_data(json_msg["b"], (6, 2))
                    module_uuid = unpacked_data[0]
                    module_version_digits = unpacked_data[1]
                    module_type = get_module_type_from_uuid(module_uuid)
                    if module_type in ["network", "camera"]:
                        module_version = [
                            str((module_version_digits & 0xE000) >> 13),    # major
                            str((module_version_digits & 0x1F00) >> 8),     # minor
                            str(module_version_digits & 0x00FF)             # patch
                        ]
                        return module_uuid , ".".join(module_version), module_type == "network"
                elif json_msg["c"] == 0x0A:
                    module_uuid = unpack_data(json_msg["b"], (6, 2))[0]
                    module_type = get_module_type_from_uuid(module_uuid)
                    if module_type in ["network", "camera"]:
                        return module_uuid , None, module_type == "network"
            except json.decoder.JSONDecodeError as jde:
                self.__print("json parse error: " + str(jde))
            except Exception:
                pass

            time.sleep(0.2)

    def send_request_network_uuid(self):
        send_pkt = parse_message(0x28, 0xFFF, 0xFFF, (0xFF, 0xFF))
        if self.is_open:
            self.write(send_pkt.encode("utf8"))

    def send_set_network_module_state(self, did, module_state, pnp_state):
        send_pkt = parse_message(0xA4, 0, did, (module_state, pnp_state))
        if self.is_open:
            self.write(send_pkt.encode("utf8"))

    def send_set_module_state(self, did, module_state, pnp_state):
        send_pkt = parse_message(0x09, 0, did, (module_state, pnp_state))
        if self.is_open:
            self.write(send_pkt.encode("utf8"))

    def send_firmware_command(self, operation_type, module_id, crc_val, page_addr):
        rot_scmd = 2 if operation_type == "erase" else 1

        cmd = 0x0D

        """ SID is 12-bits length in MODI CAN.
            To fully utilize its capacity, we split 12-bits into 4 and 8 bits.
            First 4 bits include rot_scmd information.
            And the remaining bits represent rot_stype.
        """
        sid = (rot_scmd << 8) | 1
        did = module_id

        """ The firmware command data to be sent is 8-bytes length.
            Where the first 4 bytes consist of CRC-32 information.
            Last 4 bytes represent page address information.
        """
        crc32_and_page_addr_data = bytearray(8)
        for i in range(4):
            crc32_and_page_addr_data[i] = crc_val & 0xFF
            crc_val >>= 8
            crc32_and_page_addr_data[4 + i] = page_addr & 0xFF
            page_addr >>= 8
        data = crc32_and_page_addr_data

        send_pkt = parse_message(cmd, sid, did, data)
        if self.is_open:
            self.write(send_pkt.encode("utf8"))

    def receive_firmware_command_response(self, delay=0.001, timeout=5):
        response_wait_time = time.time()
        while True:
            responese_success = False
            response_error = False

            recved = self.wait_for_json(timeout)

            if time.time() - response_wait_time > timeout or not recved:
                return False

            try:
                if not recved:
                    continue

                json_msg = json.loads(recved)
                if json_msg["c"] == 0x0C:
                    message_decoded = unpack_data(json_msg["b"], (4, 1))
                    stream_state = message_decoded[1]
                    if stream_state == self.CRC_ERROR or stream_state == self.ERASE_ERROR:
                        response_error = True
                    elif stream_state == self.CRC_COMPLETE or stream_state == self.ERASE_COMPLETE:
                        responese_success = True
            except json.decoder.JSONDecodeError as jde:
                self.__print("json parse error: " + str(jde))

            if responese_success:
                return True

            if response_error:
                return False

            time.sleep(delay)

    def send_firmware_data(self, module_id, seq_num, bin_data):
        cmd = 0x0B
        sid = seq_num
        did = module_id
        data = bytes(bin_data)
        send_pkt = parse_message(cmd, sid, did, data)
        if self.is_open:
            self.write(send_pkt.encode("utf8"))

    def set_firmware_command(self, oper_type, module_id, crc_val, page_addr):
        self.send_firmware_command(oper_type, module_id, crc_val, page_addr)
        ret = self.receive_firmware_command_response()
        if not ret and oper_type == "erase":
            retry_count = 0
            max_retry = 5
            while not ret:
                self.send_firmware_command(oper_type, module_id, crc_val, page_addr)
                ret = self.receive_firmware_command_response()
                retry_count += 1
                if retry_count > max_retry:
                    break
        return ret

    def set_firmware_data(self, module_id, seq_num, bin_data, checksum):
        self.send_firmware_data(module_id, seq_num, bin_data)
        return self.calc_crc64(bin_data, checksum)

    def set_end_flash_data(self, module_id, end_flash_data):
        end_flash_success = False
        page_retry_count = 0
        page_retry_max_count = 10
        erase_page_num = 2

        while not end_flash_success:
            # Erase page (send erase request and receive erase response)
            erase_page_success = self.set_firmware_command(
                oper_type="erase",
                module_id=module_id,
                crc_val=erase_page_num,
                page_addr=0x0801F800
            )

            # erase_page_success = self.set_firmware_command("erase", module_id, 0, 0x0801F800)
            if not erase_page_success:
                self.update_error = -1
                self.update_error_message = "End erase error"
                return False

            # Send data
            checksum = 0
            for end_flash_ptr in range(0, len(end_flash_data), 8):
                curr_data = end_flash_data[end_flash_ptr : end_flash_ptr + 8]
                checksum = self.set_firmware_data(
                    module_id,
                    seq_num=end_flash_ptr // 8,
                    bin_data=curr_data,
                    checksum=checksum
                )
                time.sleep(0.001)
            # checksum = self.set_firmware_data(module_id, 0, end_flash_data, 0)

            # CRC on current page (send CRC request and receive CRC response)
            crc_page_success = self.set_firmware_command("crc", module_id, checksum, 0x0801F800)
            if not crc_page_success:
                if self.update_error == -1:
                    return False
                else:
                    page_retry_count += 1
                    if page_retry_count > page_retry_max_count:
                        self.update_error = -1
                        self.update_error_message = "End crc error"
                        return False
                    continue
            else:
                page_retry_count = 0

            end_flash_success = True
        self.__print(f"End flash is written for network ({module_id})")
        return True

    def update_module_firmware(self, firmware_version_info):
        self.__print("update_module_firmware")
        self.update_in_progress = True
        self.progress = 0
        self.firmware_version_info = firmware_version_info

        self.__print("get network info")
        self.network_uuid, self.network_version, self.is_network = self.get_connected_module_info()

        if self.network_uuid:
            self.network_id = self.network_uuid & 0xFFF
        else:
            self.network_id = 0xFFF

        self.__print("set network module to bootloader")
        self.send_set_network_module_state(self.network_id, Module.UPDATE_FIRMWARE, Module.PNP_OFF)
        time.sleep(0.2)

        self.close()
        time.sleep(5)

        # reconnect
        self.open(self.port)
        time.sleep(2)

        # wait warning flag
        self.__print("wait warning state")
        timeout = 10
        init_time = time.time()
        is_timeout = False
        retry = 0
        max_retry = 5
        while True:
            recved = self.wait_for_json()
            if not recved:
                retry += 1
                if retry > max_retry:
                    is_timeout = True
                    break
                continue

            if time.time() - init_time > timeout or not recved:
                is_timeout = True
                break

            try:
                json_msg = json.loads(recved)
                if json_msg["c"] == 0x0A:
                    unpacked_data = unpack_data(json_msg["b"], (6, 1))
                    module_uuid = unpacked_data[0]
                    warning_type = unpacked_data[1]
                    module_type = get_module_type_from_uuid(module_uuid)
                    if module_type in ["network", "camera"]:
                        if not self.network_uuid:
                            self.network_uuid = module_uuid
                            self.network_id = self.network_uuid & 0xFFF

                        self.is_network = (module_type == "network")

                        if warning_type != 2:
                            self.send_set_module_state(self.network_id, Module.UPDATE_FIRMWARE_READY, Module.PNP_OFF)
                        if warning_type == 2:
                            break
            except json.decoder.JSONDecodeError as jde:
                self.__print("json parse error: " + str(jde))

            time.sleep(0.01)

        if is_timeout:
            self.update_in_progress = False
            self.update_error = -1
            self.update_error_message = "Warning timeout"
            self.close()
            return

        # update network module
        self.__print("update network module")
        if self.is_network:
            update_success = self.update_network_module(self.network_id)
        else:
            update_success = self.update_camera_module(self.network_id)

        self.close()

        if not update_success:
            self.__print("update error - " + self.update_error_message)
            self.update_error = -1
        else:
            self.update_error = 1

        self.update_in_progress = False

    def update_network_module(self, module_id):
        root_path = path.join(self.module_firmware_path, "network", "e103", self.firmware_version_info["network"]["app"])
        bin_path = path.join(root_path, "network.bin")
        with open(bin_path, "rb") as bin_file:
            bin_buffer = bin_file.read()

        # Init metadata of the bytes loaded
        page_size = 0x800
        flash_memory_addr = 0x08000000
        erase_page_num = 2

        bin_size = sys.getsizeof(bin_buffer)
        bin_begin = page_size
        bin_end = bin_size - ((bin_size - bin_begin) % page_size)

        page_offset = 0x8800
        page_begin = bin_begin

        erase_error_limit = 2
        erase_error_count = 0
        crc_error_limit = 2
        crc_error_count = 0
        while page_begin < bin_end :
            progress = 100 * page_begin // bin_end
            self.progress = progress

            self.__print(f"\rUpdating network ({module_id}) {self.__progress_bar(page_begin, bin_end)} {progress}%", end="")

            page_end = page_begin + page_size
            curr_page = bin_buffer[page_begin:page_end]

            # Skip current page if empty
            if curr_page == bytes(len(curr_page)):
                page_begin = page_begin + page_size
                time.sleep(0.02)
                continue

            erase_page_success = self.set_firmware_command(
                oper_type="erase",
                module_id=module_id,
                crc_val=erase_page_num,
                page_addr=flash_memory_addr + page_begin + page_offset
            )

            if not erase_page_success:
                erase_error_count = erase_error_count + 1
                if erase_error_count > erase_error_limit:
                    erase_error_count = 0
                    self.has_update_error = True
                    self.update_error_message = f"network ({module_id}) erase flash failed."
                    break
                continue
            else:
                erase_error_count = 0

            checksum = 0
            for curr_ptr in range(0, page_size, 8):
                if page_begin + curr_ptr >= bin_size:
                    break

                curr_data = curr_page[curr_ptr : curr_ptr + 8]
                checksum = self.set_firmware_data(module_id, curr_ptr // 8, curr_data, checksum)
                delay(0.001)

            # CRC on current page (send CRC request / receive CRC response)
            crc_page_success = self.set_firmware_command(
                oper_type="crc",
                module_id=module_id,
                crc_val=checksum,
                page_addr=flash_memory_addr + page_begin + page_offset
            )

            if crc_page_success:
                crc_error_count = 0
            else:
                crc_error_count = crc_error_count + 1
                if crc_error_count > crc_error_limit:
                    crc_error_count = 0
                    self.has_update_error = True
                    self.update_error_message = "Check crc failed."
                    break
                continue

            page_begin = page_begin + page_size
            time.sleep(0.01)

        self.progress = 99
        self.__print(f"\rUpdating network ({module_id}) {self.__progress_bar(99, 100)} 99%")

        verify_header = 0xAA
        if self.has_update_error:
            verify_header = 0xFF

        # Get version info from version_path, using appropriate methods
        network_version_info = self.firmware_version_info["network"]["app"]
        network_version_info = network_version_info.lstrip("v").split("-")[0]
        network_version_digits = [int(digit) for digit in network_version_info.split(".")]
        network_version = (
            network_version_digits[0] << 13
            | network_version_digits[1] << 8
            | network_version_digits[2]
        )

        # Set end-flash data to be sent at the end of the firmware update
        end_flash_data = bytearray(16)
        end_flash_data[0] = verify_header
        end_flash_data[6] = network_version & 0xFF
        end_flash_data[7] = (network_version >> 8) & 0xFF

        for xxx in range(4):
            end_flash_data[xxx + 12] = ((0x08009000 >> (xxx * 8)) & 0xFF)

        success_end_flash = self.set_end_flash_data(module_id, end_flash_data)
        if not success_end_flash:
            self.update_error_message = "version writing failed."
            self.has_update_error = True
            print(self.update_error_message)

        self.__print(f"Version info (v{network_version_info}) has been written to its firmware!")

        # Firmware update flag down, resetting used flags
        self.__print(f"Firmware update is done for network ({module_id})")

        # Reboot all connected modules
        self.send_set_module_state(0xFFF, Module.REBOOT, Module.PNP_OFF)
        self.__print("Reboot message has been sent to all connected modules")

        time.sleep(1)

        self.progress = 100
        self.__print(f"\rUpdating network ({module_id}) {self.__progress_bar(100, 100)} 100%")
        self.__print("Module firmwares have been updated!")

        time.sleep(1)

        self.close()

        return not self.has_update_error

    def update_camera_module(self, module_id):
        root_path = path.join(self.module_firmware_path, "camera", "e103", self.firmware_version_info["camera"]["app"])
        bin_path = path.join(root_path, "camera.bin")
        with open(bin_path, "rb") as bin_file:
            bin_buffer = bin_file.read()

        # Init metadata of the bytes loaded
        page_size = 0x800
        flash_memory_addr = 0x08000000
        erase_page_num = 2

        bin_size = sys.getsizeof(bin_buffer)
        bin_begin = page_size
        bin_end = bin_size - ((bin_size - bin_begin) % page_size)

        page_offset = 0x8800
        page_begin = bin_begin

        erase_error_limit = 2
        erase_error_count = 0
        crc_error_limit = 2
        crc_error_count = 0
        while page_begin < bin_end :
            progress = 100 * page_begin // bin_end
            self.progress = progress

            self.__print(f"\rUpdating camera ({module_id}) {self.__progress_bar(page_begin, bin_end)} {progress}%", end="")

            page_end = page_begin + page_size
            curr_page = bin_buffer[page_begin:page_end]

            # Skip current page if empty
            if curr_page == bytes(len(curr_page)):
                page_begin = page_begin + page_size
                time.sleep(0.02)
                continue

            erase_page_success = self.set_firmware_command(
                oper_type="erase",
                module_id=module_id,
                crc_val=erase_page_num,
                page_addr=flash_memory_addr + page_begin + page_offset
            )

            if not erase_page_success:
                erase_error_count = erase_error_count + 1
                if erase_error_count > erase_error_limit:
                    erase_error_count = 0
                    self.has_update_error = True
                    self.update_error_message = f"camera ({module_id}) erase flash failed."
                    break
                continue
            else:
                erase_error_count = 0

            checksum = 0
            for curr_ptr in range(0, page_size, 8):
                if page_begin + curr_ptr >= bin_size:
                    break

                curr_data = curr_page[curr_ptr : curr_ptr + 8]
                checksum = self.set_firmware_data(module_id, curr_ptr // 8, curr_data, checksum)
                delay(0.001)

            # CRC on current page (send CRC request / receive CRC response)
            crc_page_success = self.set_firmware_command(
                oper_type="crc",
                module_id=module_id,
                crc_val=checksum,
                page_addr=flash_memory_addr + page_begin + page_offset
            )

            if crc_page_success:
                crc_error_count = 0
            else:
                crc_error_count = crc_error_count + 1
                if crc_error_count > crc_error_limit:
                    crc_error_count = 0
                    self.has_update_error = True
                    self.update_error_message = "Check crc failed."
                    break
                continue

            page_begin = page_begin + page_size
            time.sleep(0.01)

        self.progress = 99
        self.__print(f"\rUpdating camera ({module_id}) {self.__progress_bar(99, 100)} 99%")

        verify_header = 0xAA
        if self.has_update_error:
            verify_header = 0xFF

        # Get version info from version_path, using appropriate methods
        camera_version_info = self.firmware_version_info["camera"]["app"]
        camera_version_info = camera_version_info.lstrip("v").split("-")[0]
        camera_version_digits = [int(digit) for digit in camera_version_info.split(".")]
        camera_version = (
            camera_version_digits[0] << 13
            | camera_version_digits[1] << 8
            | camera_version_digits[2]
        )

        # Set end-flash data to be sent at the end of the firmware update
        end_flash_data = bytearray(16)
        end_flash_data[0] = verify_header
        end_flash_data[6] = camera_version & 0xFF
        end_flash_data[7] = (camera_version >> 8) & 0xFF

        for xxx in range(4):
            end_flash_data[xxx + 12] = ((0x08009000 >> (xxx * 8)) & 0xFF)

        success_end_flash = self.set_end_flash_data(module_id, end_flash_data)
        if not success_end_flash:
            self.update_error_message = "version writing failed."
            self.has_update_error = True
            print(self.update_error_message)

        self.__print(f"Version info (v{camera_version_info}) has been written to its firmware!")

        # Firmware update flag down, resetting used flags
        self.__print(f"Firmware update is done for camera ({module_id})")

        # Reboot all connected modules
        self.send_set_module_state(0xFFF, Module.REBOOT, Module.PNP_OFF)
        self.__print("Reboot message has been sent to all connected modules")

        time.sleep(1)

        self.progress = 100
        self.__print(f"\rUpdating camera ({module_id}) {self.__progress_bar(100, 100)} 100%")
        self.__print("Module firmwares have been updated!")

        time.sleep(1)

        self.close()

        return not self.has_update_error

    def read_json(self):
        json_pkt = b""
        while json_pkt != b"{":
            if not self.is_open:
                return None
            json_pkt = self.read()
            if json_pkt == b"":
                return None
            time.sleep(0.001)
        json_pkt += self.read_until(b"}")
        return json_pkt.decode("utf8")

    def wait_for_json(self, timeout=2):
        json_msg = self.read_json()
        init_time = time.time()
        while not json_msg:
            json_msg = self.read_json()
            time.sleep(0.001)
            if time.time() - init_time > timeout:
                return None
        return json_msg

    def calc_crc32(self, data: bytes, crc: int) -> int:
        crc ^= int.from_bytes(data, byteorder="little", signed=False)

        for _ in range(32):
            if crc & (1 << 31) != 0:
                crc = (crc << 1) ^ 0x4C11DB7
            else:
                crc <<= 1
            crc &= 0xFFFFFFFF

        return crc

    def calc_crc64(self, data, checksum):
        checksum = self.calc_crc32(data[:4], checksum)
        checksum = self.calc_crc32(data[4:], checksum)
        return checksum

    def __progress_bar(self, current, total):
        curr_bar = 50 * current // total
        rest_bar = 50 - curr_bar
        return f"[{'=' * curr_bar}>{'.' * rest_bar}]"

    def __print(self, data, end="\n"):
        if self.print:
            print(data, end)


class NetworkFirmwareMultiUpdater():
    def __init__(self, module_firmware_path):
        self.update_in_progress = False
        self.ui = None
        self.list_ui = None
        self.task_end_callback = None
        self.module_firmware_path = module_firmware_path

    def set_ui(self, ui, list_ui=None):
        self.ui = ui
        self.list_ui = list_ui

    def set_task_end_callback(self, task_end_callback):
        self.task_end_callback = task_end_callback

    def update_module_firmware(self, modi_ports, firmware_version_info={}):
        self.network_updaters = []
        self.network_uuid = []
        self.state = []

        for i, modi_port in enumerate(modi_ports):
            if i > 9:
                break
            try:
                network_updater = NetworkFirmwareUpdater(
                    device=modi_port,
                    module_firmware_path=self.module_firmware_path
                )
                network_updater.set_print(False)
                network_updater.set_raise_error(False)
            except Exception:
                print("open " + modi_port + " error")
            else:
                self.network_updaters.append(network_updater)
                self.state.append(0)
                self.network_uuid.append('')

        if self.list_ui:
            self.list_ui.set_device_num(len(self.network_updaters))
            self.list_ui.ui.close_button.setEnabled(False)

        self.update_in_progress = True

        for index, network_updater in enumerate(self.network_updaters):
            th.Thread(
                target=network_updater.update_module_firmware,
                args=(firmware_version_info, ),
                daemon=True
            ).start()

        if self.ui:
            if self.ui.is_english:
                self.ui.update_network_module_button.setText("Network/Camera module update is in progress. (0%)")
            else:
                self.ui.update_network_module_button.setText("네트워크/카메라 모듈 업데이트가 진행중입니다. (0%)")

        delay = 0.01
        while True:
            is_done = True
            total_progress = 0
            for index, network_updater in enumerate(self.network_updaters):
                if network_updater.network_uuid and len(self.network_uuid[index]) == 0:
                    self.network_uuid[index] = f'0x{network_updater.network_uuid:X}'
                    if self.list_ui:
                        self.list_ui.network_uuid_signal.emit(index, self.network_uuid[index])

                if self.state[index] == 0:
                    # update modules
                    is_done = is_done & False
                    if network_updater.update_error == 0:
                        current_module_progress = network_updater.progress
                        total_progress += current_module_progress / len(self.network_updaters)

                        if self.list_ui:
                            if len(self.network_uuid[index]):
                                self.list_ui.network_uuid_signal.emit(index, self.network_uuid[index])
                            self.list_ui.progress_signal.emit(index, int(current_module_progress))
                    else:
                        total_progress += 100 / len(self.network_updaters)
                        self.state[index] = 1
                elif self.state[index] == 1:
                    # end
                    total_progress += 100 / len(self.network_updaters)
                    if network_updater.update_error == 1:
                        # update success
                        if self.list_ui:
                            self.list_ui.network_state_signal.emit(index, 0)
                            self.list_ui.progress_signal.emit(index, 100)
                    else:
                        print("\n" + network_updater.update_error_message + "\n")
                        # update error
                        if self.list_ui:
                            self.list_ui.network_state_signal.emit(index, -1)
                            self.list_ui.error_message_signal.emit(index, network_updater.update_error_message)

                    self.state[index] = 2
                elif self.state[index] == 2:
                    total_progress += 100 / len(self.network_updaters)

                time.sleep(0.001)

            if len(self.network_updaters):
                print(f"{self.__progress_bar(total_progress, 100)}", end="")
                if self.ui:
                    if self.ui.is_english:
                        self.ui.update_network_module_button.setText(f"Network/Camera module update is in progress. ({int(total_progress)}%)")
                    else:
                        self.ui.update_network_module_button.setText(f"네트워크/카메라 모듈 업데이트가 진행중입니다. ({int(total_progress)}%)")

                if self.list_ui:
                    self.list_ui.total_progress_signal.emit(int(total_progress))
                    self.list_ui.total_status_signal.emit("Update...")

            if is_done:
                break

            time.sleep(delay)

        self.update_in_progress = False

        if self.task_end_callback:
            self.task_end_callback(self.list_ui)

        print("\nFirmware update is complete!!")

    @staticmethod
    def __progress_bar(current: int, total: int) -> str:
        curr_bar = int(50 * current // total)
        rest_bar = int(50 - curr_bar)
        return (f"\rFirmware Update: [{'=' * curr_bar}>{'.' * rest_bar}] {100 * current / total:3.1f}%")
