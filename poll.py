"""
poll.py

This code designed to do the following things:
1. Start a tcp server which broadcasts customized data string to each clients that connected to the tcp server
2. Start an ntrip client that communicates with a VRS NTRIP server
3. Configure and polling the data from Zed-F9P RTK GPS Module

Usage:

python3 poll.py config_file=path_to_config_file.yaml

Press CTRL-C to terminate.

FYI: Since Python implements a Global Interpreter Lock (GIL),
threads are not strictly concurrent, though this is of minor
practical consequence here.

Created 29 Nov 2024

:author: Sami Fauzan R
:license: BSD 3-Clause (following pygnssutils license)
"""

from queue import Queue, Empty
from sys import argv
from os import getenv, getcwd
from threading import Event, Thread
from datetime import datetime
from time import sleep, time
from logging import getLogger, basicConfig
from collections import deque
from decimal import Decimal, getcontext

from serial import Serial

from pyubx2 import (
    POLL, 
    SET_LAYER_BBR,
    SET_LAYER_RAM,
    SET_LAYER_FLASH,
    UBX_PAYLOADS_POLL, 
    UBX_PROTOCOL, 
    NMEA_PROTOCOL, 
    UBXMessage, 
    UBXReader
)

from pygnssutils import VERBOSITY_HIGH, VERBOSITY_DEBUG, set_logging

from ntripclient import GNSSNTRIPClient
from tcp import TCPServer

getcontext().prec = 9

cwd = getcwd()
log_file = f"/var/log/rtkgps/rtkgps-{datetime.now().strftime('%Y%m%d%H%M%S')}.log"

logger = getLogger("rtkgps")
set_logging(getLogger("ntripclient"), VERBOSITY_HIGH, logtofile=log_file)
set_logging(getLogger("tcp"), VERBOSITY_HIGH, logtofile=log_file)
set_logging(getLogger("rtkgps"), VERBOSITY_HIGH, logtofile=log_file)

RETRY_INTERVAL = 2
ntrip_client = None

def io_data(
    stream: object,
    ubr: UBXReader,
    readqueue: Queue,
    sendqueue: Queue,
    stop: Event,
):
    """
    THREADED
    Read and parse inbound NMEA data and place
    raw and parsed data on queue.

    Send any queued outbound messages to receiver.
    """
    # pylint: disable=broad-exception-caught

    while not stop.is_set():
        try:
            if stream.in_waiting:
                (raw_data, parsed_data) = ubr.read()
                
                if parsed_data:
                    readqueue.put((raw_data, parsed_data))

            if not sendqueue.empty():
                data = sendqueue.get(False)
                if data is not None:
                    if type(data) is tuple:
                        raw, parsed = data
                        ubr.datastream.write(raw)
                    else:
                        ubr.datastream.write(data.serialize())
                sendqueue.task_done()

        except Exception as err:
            logger.info(f"\n\nSomething went wrong {err}\n\n")
            continue

def process_data(gga_queue: Queue, confirm_queue: Queue, data_queue: Queue, gps_queue: Queue, stop: Event):
    """
    THREADED
    Get NMEA data from data_queue and process.
    """

    # Initialize deques for storing most recent values
    lat = deque(maxlen=1)
    long = deque(maxlen=1)
    height = deque(maxlen=1)
    fix = deque(maxlen=1)
    numSV = deque(maxlen=1)
    heading = deque(maxlen=1)
    PDOP = deque(maxlen=1)
    HDOP = deque(maxlen=1)
    VDOP = deque(maxlen=1)
    speed = deque(maxlen=1)
    last_pdop = 0
    last_hdop = 0
    last_vdop = 0

    while not stop.is_set():
        try:
            # Attempt to get data from the queue with a small timeout
            (raw_data, parsed) = data_queue.get(timeout=0.1)
        except:
            continue

        logger.debug(f"Msg: {parsed}")

        # Handle GGA messages
        if hasattr(parsed, "msgID") and parsed.msgID == "GGA":
            logger.info(f"Fix type: {parsed.quality}")
            fix.append(parsed.quality)
            gga_queue.put((raw_data, parsed))
            numSV.append(parsed.numSV)

        else:
            if parsed.identity[0:3] == "ACK":
                confirm_queue.put(parsed.identity)

            # Update heading motion values if available
            if parsed.identity == "NAV-HPPOSLLH":
                lat.append(parsed.lat)
                long.append(parsed.lon)
                height.append(parsed.hMSL)

            # Update heading motion values if available
            if parsed.identity == "NAV-PVT":
                heading.append(parsed.headMot)
                speed.append(parsed.gSpeed)

            # Update DOP values if available
            if parsed.identity == "NAV-DOP":
                PDOP.append(parsed.pDOP)
                HDOP.append(parsed.hDOP)
                VDOP.append(parsed.vDOP)
                last_pdop = parsed.pDOP
                last_hdop = parsed.hDOP
                last_vdop = parsed.vDOP
            else:
                PDOP.append(last_pdop)
                HDOP.append(last_hdop)
                VDOP.append(last_vdop)

        
        data_queue.task_done()

        # Ensure all deques have data before putting into gps_queue
        deq = [lat, long, height, fix, numSV, heading, speed, PDOP, HDOP, VDOP]
        
        count = 0
        for val in deq:
            if len(val) == 0:
                count = 1
        
        if count == 0:
            gps_queue.put((lat, long, height, fix, numSV, heading, speed, PDOP, HDOP, VDOP))
                
def ntrip(gga_queue: Queue, send_queue: Queue, stop: Event, kwargs):
    server = kwargs.get("server", "69.64.185.41")
    port = int(kwargs.get("port", 7801))
    mountpoint = kwargs.get("mountpoint", "MSM5")
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
        stopevent=stop
    )
    
    return gnc

def broadcast(tcp_server: TCPServer, gps_data_queue: Queue, ntrip_client: GNSSNTRIPClient, stop: Event):
    last_broadcast_time = None
    last_data = None
    
    # Moving average setup
    window_size = 10  # Use the last 10 intervals for the moving average
    recent_intervals = deque(maxlen=window_size)
    
    while not stop.is_set():
        current_time = time()
        
        # Enforce minimum rate of 1 Hz (1 message per second)
        if last_broadcast_time is not None and (current_time - last_broadcast_time > 1):
            if last_data is not None:
                tcp_server.broadcast(message=last_data)
                logger.warning(f"Minimum rate enforced: Broadcasting last known data: {last_data}")
                # Update last broadcast time
                last_broadcast_time = current_time
                # Record the interval for moving average
                recent_intervals.append(1.0)  # Assume 1 second since last broadcast
        
        if not gps_data_queue.empty():
            connect = "ON" if ntrip_client.connected else "OFF"
            lat, long, height, fix, numSV, heading, speed, PDOP, HDOP, VDOP = gps_data_queue.get()
            count = 0
            for val in lat, long, height, fix, numSV, heading, speed, PDOP, HDOP, VDOP:
                if len(val) == 0:
                    count = 1

            if count == 0:
                height_m = height.pop() / 1000
                long_data = Decimal(long.pop())
                lat_data = Decimal(lat.pop())
                heading_data = heading.pop()
                speed_data = speed.pop() / 1000  # in meter/second
                    
                type = fix.pop()
                # Using match-case for fixtype determination
                match type:
                    case 1:
                        fixtype = "GPS"
                    case 2:
                        fixtype = "DGPS"
                    case 3:
                        fixtype = "3D"
                    case 4:
                        fixtype = "FIX"
                    case 5:
                        fixtype = "Float"
                    case _:
                        fixtype = str(type)
                
                message = f"{lat_data:.9f},{long_data:.9f},{height_m:.4f},{fixtype},{numSV.pop()},{PDOP.pop()},{HDOP.pop()},{VDOP.pop()},{heading_data:.1f},{speed_data:.1f},{connect}" + "\r\n"
                last_data = message
                tcp_server.broadcast(message=last_data)
                logger.info(f"Broadcasting to tcp clients: {last_data}")
                
                # Calculate interval and update moving average
                if last_broadcast_time is not None:
                    interval = current_time - last_broadcast_time
                    recent_intervals.append(interval)
                last_broadcast_time = current_time
                
                # Calculate rates
                if len(recent_intervals) > 0:
                    moving_avg_rate = len(recent_intervals) / sum(recent_intervals)  # messages per second
                else:
                    moving_avg_rate = 0
                
                logger.info(f"Moving Avg Rate: {moving_avg_rate:.2f} msg/sec")
                
                gps_data_queue.task_done()
            else:
                gps_data_queue.task_done()
                continue
            
def config():
    layer = SET_LAYER_RAM
    configs = [
                ("CFG_MSGOUT_NMEA_ID_GSV_USB", 0),
                ("CFG_MSGOUT_NMEA_ID_GSA_USB", 0),
                ("CFG_MSGOUT_NMEA_ID_GGA_USB", 1),
                ("CFG_MSGOUT_NMEA_ID_GLL_USB", 0),
                ("CFG_MSGOUT_NMEA_ID_VTG_USB", 0),
                ("CFG_MSGOUT_NMEA_ID_RMC_USB", 0),
                ("CFG_MSGOUT_UBX_NAV_DOP_USB", 1),
                ("CFG_MSGOUT_UBX_NAV_HPPOSLLH_USB", 1),
                ("CFG_MSGOUT_UBX_NAV_POSLLH_USB", 0),
                ("CFG_MSGOUT_UBX_NAV_STATUS_USB", 0),
                ("CFG_MSGOUT_UBX_NAV_PVT_USB", 1),
                ("CFG_RATE_MEAS", 100),
                ("CFG_RATE_NAV", 2),
            ]
    msg_ram = UBXMessage.config_set(layer, transaction=0, cfgData=configs)
    layer = SET_LAYER_BBR
    msg_bbr = UBXMessage.config_set(layer, transaction=0, cfgData=configs)
    layer = SET_LAYER_FLASH
    msg_flash = UBXMessage.config_set(layer, transaction=0, cfgData=configs)
    return msg_ram, msg_bbr, msg_flash
    
def connect_to_serial(port, baudrate, timeout):
    """
    Attempts to connect to the serial port with retry logic.
    """
    while True:
        try:
            logger.info(f"Attempting to connect to {port}...")
            return Serial(port, baudrate, timeout=timeout)
        except Exception as e:
            logger.info(f"Connection to {e} is failed")
            return None

def main(**kwargs):
    """
    Main routine.
    """

    port = kwargs.get("serport", "/dev/f9pdev")
    baudrate = int(kwargs.get("baudrate", 57600))
    timeout = float(kwargs.get("timeout", 1))

    with connect_to_serial(port, baudrate, timeout=timeout) as serial_stream:
        if serial_stream is None:
            return 0
        ubxreader = UBXReader(
            serial_stream, 
            protfilter= UBX_PROTOCOL | NMEA_PROTOCOL)

        read_queue = Queue()
        gga_queue = Queue()
        gps_queue = Queue()
        config_queue = Queue()
        send_queue = Queue()
        stop_event = Event()

        logger.info("Starting tcp server...")

        tcp_server = TCPServer(host="0.0.0.0",port=5051)

        server_thread = Thread(target=tcp_server.start)
        server_thread.daemon = True
        server_thread.start()


        io_thread = Thread(
            target=io_data,
            args=(
                serial_stream,
                ubxreader,
                read_queue,
                send_queue,
                stop_event,
            ),
        )

        process_thread = Thread(
            target=process_data,
            args=(
                gga_queue,
                config_queue,
                read_queue,
                gps_queue,
                stop_event,
            ),
        )
        logger.info("\nStarting handler threads. Press Ctrl-C to terminate...")
        logger.info("Starting io thread...")
        io_thread.start()
        logger.info("Starting process thread...")
        process_thread.start()

        logger.info("Starting ntrip client thread...")
        ntrip_client = ntrip(gga_queue=gga_queue, send_queue=send_queue, stop=stop_event, kwargs=kwargs)
    
        broadcast_thread = Thread(
            target=broadcast,
            args=(
                tcp_server,
                gps_queue,
                ntrip_client,
                stop_event
            )
        )
        logger.info("Starting broadcast thread...")
        broadcast_thread.start()

        f9p_ready = False
        config_success = 0

        # loop until user presses Ctrl-C
        while not stop_event.is_set():
            try:
                if not f9p_ready:
                    # Configure the F9P to specific parameter
                    logger.info("Configuring the F9P...")
                    set_ram, set_bbr, set_flash = config()
                    if not f9p_ready:
                        response = ""
                        while config_success <= 2:
                            layer = ""
                            match config_success:
                                case 0:
                                    layer = "RAM"
                                    send_queue.put(set_ram)
                                case 1:
                                    layer = "BBR"
                                    send_queue.put(set_bbr)
                                case 2:
                                    layer = "Flash"
                                    send_queue.put(set_flash)
                            logger.info(f"Sent config layer {layer}")
                            tries = 0
                            res = True
                            while config_queue.empty():
                                tries += 1
                                sleep(0.5)
                                if tries >= 5:
                                    res = False
                                    logger.info(f"Configuration to {layer} is unsuccessful; No response from f9p")
                                    if layer == "RAM":
                                        logger.info("Quitting ...")
                                    break
                            if res is True:
                                response = config_queue.get()
                                if response == "ACK-ACK":
                                    logger.info(f"Configuration to {layer} is successful")
                                    config_success += 1
                                if response == "ACK-NAK":
                                    logger.info(f"Configuration to {layer} is unsuccessful")
                                config_queue.task_done()
                            else:
                                stop_event.set()
                                break

                        if config_success == 3:
                            f9p_ready = True
                            sleep(0.2)
                        else:
                            config_success = 0

                #monitor ntrip client connection; rerun the client when it's disconnected
                if ntrip_client.connected != True:
                    ntrip_client = ntrip_client(gga_queue, send_queue, kwargs)
                    sleep(1)
                sleep(1)

            except KeyboardInterrupt:  # capture Ctrl-C
                logger.info("\n\nTerminated by user.")
                stop_event.set()

        logger.info("\nStop signal set. Waiting for threads to complete...")
        io_thread.join()
        process_thread.join()
        logger.info("\nProcessing complete")


if __name__ == "__main__":

    main(**dict(arg.split("=") for arg in argv[1:]))