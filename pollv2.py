from multiprocessing import Process, Event, Queue
from collections import deque
from time import sleep, time
from logging import getLogger
from os import getenv
from serial import Serial
from pyubx2 import UBXReader, UBXMessage, UBX_PROTOCOL, NMEA_PROTOCOL
from pygnssutils import set_logging
from ntripclient import GNSSNTRIPClient
from tcp import TCPServer

logger = getLogger("rtkgps")
set_logging(getLogger("tcp"), "HIGH")
set_logging(getLogger("rtkgps"), "HIGH")


def io_data(stream, ubr, readqueue, sendqueue, stop_event):
    while not stop_event.is_set():
        try:
            if stream.in_waiting:
                raw_data, parsed_data = ubr.read()
                if parsed_data:
                    readqueue.put((raw_data, parsed_data))

            if not sendqueue.empty():
                data = sendqueue.get(False)
                if data is not None:
                    if isinstance(data, tuple):
                        raw, parsed = data
                        ubr.datastream.write(raw)
                    else:
                        ubr.datastream.write(data.serialize())
                sendqueue.task_done()

        except Exception as err:
            logger.info(f"Something went wrong: {err}")
            continue


def process_data(gga_queue, data_queue, gps_queue, stop_event):
    lat, long, height, fix = deque(maxlen=1), deque(maxlen=1), deque(maxlen=1), deque(maxlen=1)
    PDOP, HDOP, VDOP = deque(maxlen=1), deque(maxlen=1), deque(maxlen=1)
    last_pdop, last_hdop, last_vdop = 0, 0, 0
    hppos, timeout, last_hppos = False, 1, time()

    while not stop_event.is_set():
        try:
            raw_data, parsed = data_queue.get(timeout=0.1)
        except:
            continue

        logger.debug(f"Msg: {parsed}")
        if hasattr(parsed, "lat"):
            if time() - last_hppos > timeout:
                hppos = False

            if hasattr(parsed, "invalidLlh") and parsed.invalidLlh != 1:
                lat.append(parsed.lat)
                long.append(parsed.lon)
                height.append(parsed.height)
                hppos = True
                last_hppos = time()
            elif not hppos:
                lat.append(parsed.lat)
                long.append(parsed.lon)

        if hasattr(parsed, "pDOP"):
            PDOP.append(parsed.pDOP)
            HDOP.append(parsed.hDOP)
            VDOP.append(parsed.vDOP)
            last_pdop, last_hdop, last_vdop = parsed.pDOP, parsed.hDOP, parsed.vDOP
        else:
            PDOP.append(last_pdop)
            HDOP.append(last_hdop)
            VDOP.append(last_vdop)

        if hasattr(parsed, "msgID") and parsed.msgID == "GGA":
            logger.info(f"Fix type: {parsed.quality}")
            fix.append(parsed.quality)
            gga_queue.put((raw_data, parsed))

        data_queue.task_done()

        if all(len(deque_) > 0 for deque_ in [lat, long, height, fix, PDOP, HDOP, VDOP]):
            gps_queue.put((lat, long, height, fix, PDOP, HDOP, VDOP))


def ntrip(gga_queue, send_queue, kwargs):
    server = kwargs.get("server", "69.64.185.41")
    port = int(kwargs.get("port", 7801))
    mountpoint = kwargs.get("mountpoint", "MSM4")
    user = kwargs.get("user", getenv("PYGPSCLIENT_USER", "grk28"))
    password = kwargs.get("password", getenv("PYGPSCLIENT_PASSWORD", "730d2"))

    gnc = GNSSNTRIPClient()
    gnc.run(
        server=server,
        port=port,
        https=0,
        mountpoint=mountpoint,
        datatype="RTCM",
        ntripuser=user,
        ntrippassword=password,
        ggainterval=1,
        gga_data=gga_queue,
        output=send_queue,
    )
    return gnc


def broadcast(tcp_server, gps_data_queue, ntrip_client, stop_event):
    last_count, rate_count, last_data, seconds = time(), 0, None, 0

    while not stop_event.is_set():
        data_freq = rate_count / seconds if seconds != 0 else 0

        if data_freq <= 1 and last_data is not None:
            rate_count += 1
            seconds = time() - last_count
            tcp_server.broadcast(message=last_data)
            logger.info(f"Broadcasting to tcp clients: {last_data}")
            logger.info(f"{data_freq:.2f} msg per sec")

        elif not gps_data_queue.empty():
            connect = "ON" if ntrip_client.connected else "OFF"
            lat, long, height, fix, PDOP, HDOP, VDOP = gps_data_queue.get()
            if all(len(deque_) > 0 for deque_ in [lat, long, height, fix, PDOP, HDOP, VDOP]):
                fixtype = {1: "GPS", 2: "DGPS", 3: "3D", 4: "FIX", 5: "Float"}.get(fix.pop(), str(fix))
                message = f"{lat.pop()},{long.pop()},{height.pop()},{fixtype},{PDOP.pop()},{HDOP.pop()},{VDOP.pop()},{connect}\r\n"
                last_data = message
                tcp_server.broadcast(message=last_data)
                rate_count += 1
                seconds = time() - last_count
                logger.info(f"{data_freq:.2f} msg per sec")
                gps_data_queue.task_done()


def main(**kwargs):
    port = kwargs.get("serport", "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00")
    baudrate, timeout = int(kwargs.get("baudrate", 57600)), float(kwargs.get("timeout", 1))

    with Serial(port, baudrate, timeout=timeout) as serial_stream:
        ubxreader = UBXReader(serial_stream, protfilter=UBX_PROTOCOL | NMEA_PROTOCOL)

        read_queue, gga_queue, gps_queue, send_queue = Queue(), Queue(), Queue(), Queue()
        stop_event = Event()

        logger.info("Starting tcp server...")
        tcp_server = TCPServer(host="0.0.0.0", port=5051)
        server_process = Process(target=tcp_server.start, daemon=True)
        server_process.start()

        io_process = Process(target=io_data, args=(serial_stream, ubxreader, read_queue, send_queue, stop_event))
        process_process = Process(target=process_data, args=(gga_queue, read_queue, gps_queue, stop_event))
        io_process.start()
        process_process.start()

        logger.info("Starting ntrip process...")
        ntrip_client = ntrip(gga_queue, send_queue, kwargs)

        broadcast_process = Process(target=broadcast, args=(tcp_server, gps_queue, ntrip_client, stop_event))
        broadcast_process.start()

        try:
            while not stop_event.is_set():
                sleep(1)
        except KeyboardInterrupt:
            logger.info("Terminated by user.")
            stop_event.set()

        io_process.join()
        process_process.join()
        broadcast_process.join()


if __name__ == "__main__":
    main(**dict(arg.split("=") for arg in argv[1:]))