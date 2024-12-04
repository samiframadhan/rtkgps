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
from os import getenv
from threading import Event, Thread
from time import sleep, time, time_ns
from logging import getLogger
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

logger = getLogger("rtkgps")
set_logging(getLogger("ntripclient"), VERBOSITY_HIGH)
set_logging(getLogger("tcp"), VERBOSITY_HIGH)
set_logging(getLogger("rtkgps"), VERBOSITY_HIGH)
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
        deq = [lat, long, height, fix, numSV, heading, PDOP, HDOP, VDOP]
        
        count = 0
        for val in deq:
            if len(val) == 0:
                count = 1
        
        if count == 0:
            gps_queue.put((lat, long, height, fix, numSV, heading, PDOP, HDOP, VDOP))
                
def ntrip(gga_queue: Queue, send_queue: Queue, stop: Event, kwargs):
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
        stopevent=stop
    )
    
    return gnc

def broadcast(tcp_server: TCPServer, gps_data_queue: Queue, ntrip_client: GNSSNTRIPClient, stop: Event):
    init_time = time()
    rate_count = 0
    last_data = None
    data_freq = 0
    prev_broadcast = time()
    seconds = 0
    while not stop.is_set():
        
        if seconds != 0:
            seconds = time() - init_time
            data_freq = rate_count / seconds

        if data_freq <= 1 and last_data != None:
            rate_count += 1
            seconds = time() - init_time
            tcp_server.broadcast(message=last_data)
            logger.info(f"Broadcasting to tcp clients: {last_data}")
            logger.info(f"{data_freq:.2f} msg per sec")

        elif not gps_data_queue.empty():
            connect = "ON" if ntrip_client.connected == True else "OFF"
            lat, long, height, fix, numSV, heading, PDOP, HDOP, VDOP = gps_data_queue.get()
            count = 0
            for val in lat, long, height, fix, numSV, heading, PDOP, HDOP, VDOP:
                if len(val) == 0:
                    count = 1

            if count == 0:
                height_m = height.pop() / 1000
                long_data = Decimal(long.pop())
                lat_data = Decimal(lat.pop())
                heading_data = heading.pop()
                    
                type = fix.pop()
                if type == 1:
                    fixtype = "GPS"
                elif type == 2:
                    fixtype = "DGPS"
                elif type == 3:
                    fixtype = "3D"
                elif type == 4:
                    fixtype = "FIX"
                elif type == 5:
                    fixtype = "Float"
                else:
                    fixtype = str(fix)
                message = f"{lat_data:.9f},{long_data:.9f},{height_m:.4f},{fixtype},{numSV.pop()},{PDOP.pop()},{HDOP.pop()},{VDOP.pop()},{heading_data:.1f},{connect}" + "\r\n"
                last_data = message
                logger.info(f"Broadcasting to tcp clients: {last_data}")
                tcp_server.broadcast(message=last_data)
                rate_count += 1
                seconds = time() - init_time
                
                logger.info(f"{data_freq:.2f} msg per sec")
                
                gps_data_queue.task_done()
            else:
                gps_data_queue.task_done()
                continue
        

        # elif time_ns() - prev_broadcast > 1: #if last count is more than 1 sec, broadcast last message
        #     seconds = (time_ns() - prev_broadcast)/1000
        #     data_freq = 1 / seconds
        #     logger.info(f"{data_freq} msg per sec")
        #     logger.info(f"Broadcasting to tcp clients: {last_data}")
        #     tcp_server.broadcast(message=last_data)
        #     prev_broadcast = time_ns()
            
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
                ("CFG_RATE_MEAS", 80),
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
            logger.error(f"Connection to {port} failed: {e}")
            logger.info(f"Retrying in {RETRY_INTERVAL} seconds...")
            time.sleep(RETRY_INTERVAL)

def main(**kwargs):
    """
    Main routine.
    """

    port = kwargs.get("serport", "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00")
    baudrate = int(kwargs.get("baudrate", 57600))
    timeout = float(kwargs.get("timeout", 1))

    with connect_to_serial(port, baudrate, timeout=timeout) as serial_stream:
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
                    
                    response = ""
                    while response != "ACK-ACK":
                        send_queue.put(set_ram)
                        tries = 0
                        while config_queue.empty():
                            tries += 1
                            sleep(1)
                            if tries >= 5:
                                break
                        response = config_queue.get()
                        if response == "ACK-ACK":
                            logger.info("Configuration to RAM is successful")
                            config_success += 1
                        if response == "ACK-NAK":
                            logger.info("Configuration to RAM is unsuccessful")
                        config_queue.task_done()
                    
                    response = ""
                    while response != "ACK-ACK":
                        send_queue.put(set_bbr)
                        tries = 0
                        while config_queue.empty():
                            tries += 1
                            sleep(1)
                            if tries >= 5:
                                break
                        response = config_queue.get()
                        if response == "ACK-ACK":
                            logger.info("Configuration to BBR is successful")
                            config_success += 1
                        if response == "ACK-NAK":
                            logger.info("Configuration to BBR is unsuccessful")
                        config_queue.task_done()
                        
                    response = ""
                    while response != "ACK-ACK":
                        send_queue.put(set_flash)
                        tries = 0
                        while config_queue.empty():
                            tries += 1
                            sleep(1)
                            if tries >= 5:
                                break
                        response = config_queue.get()
                        if response == "ACK-ACK":
                            logger.info("Configuration to Flash is successful")
                            config_success += 1
                        if response == "ACK-NAK":
                            logger.info("Configuration to Flash is unsuccessful")
                        config_queue.task_done()

                    if config_success == 3:
                        f9p_ready = True
                        sleep(0.5)
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