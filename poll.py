"""
nmeapoller.py

This example illustrates how to read, write and display NMEA messages
"concurrently" using threads and queues. This represents a useful
generic pattern for many end user applications.

Usage:

python3 nmeapoller.py port=/dev/ttyACM0 baudrate=38400 timeout=3

It implements two threads which run concurrently:
1) an I/O thread which continuously reads NMEA data from the
receiver and sends any queued outbound command or poll messages.
2) a process thread which processes parsed NMEA data - in this example
it simply prints the parsed data to the terminal.
NMEA data is passed between threads using queues.

Press CTRL-C to terminate.

FYI: Since Python implements a Global Interpreter Lock (GIL),
threads are not strictly concurrent, though this is of minor
practical consequence here.

Created on 07 Aug 2021

:author: semuadmin
:copyright: SEMU Consulting © 2021
:license: BSD 3-Clause
"""

from queue import Queue, Empty
from sys import argv
from os import getenv
from threading import Event, Thread
from time import sleep, time
from logging import getLogger
from collections import deque

from serial import Serial

from pyubx2 import POLL, UBX_PAYLOADS_POLL, UBX_PROTOCOL, NMEA_PROTOCOL, UBXMessage, UBXReader
from pynmeagps import NMEA_MSGIDS, POLL, NMEAMessage, NMEAReader
from pygnssutils import VERBOSITY_HIGH, VERBOSITY_DEBUG, set_logging

from ntripclient import GNSSNTRIPClient
from tcp import TCPServer

logger = getLogger("rtkgps")
# set_logging(getLogger("ntripclient"), VERBOSITY_HIGH)
set_logging(getLogger("tcp"), VERBOSITY_HIGH)
set_logging(getLogger("rtkgps"), VERBOSITY_HIGH)
poll_str = ["GGA", "GLL", "GNS", "LR2", "MOB", "RMA", "RMB", "RMC", "TRF", "WPL", "BWC", "BWR"]
only_gga = ["GGA"]

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

def process_data(gga_queue: Queue, data_queue: Queue, gps_queue: Queue, stop: Event):
    """
    THREADED
    Get NMEA data from data_queue and process.
    """

    # Initialize deques for storing most recent values
    lat = deque(maxlen=1)
    long = deque(maxlen=1)
    height = deque(maxlen=1)
    fix = deque(maxlen=1)
    PDOP = deque(maxlen=1)
    HDOP = deque(maxlen=1)
    VDOP = deque(maxlen=1)
    last_pdop = 0
    last_hdop = 0
    last_vdop = 0

    hppos = False
    timeout = 1  # Timeout in seconds
    last_hppos = time()

    while not stop.is_set():
        try:
            # Attempt to get data from the queue with a small timeout
            (raw_data, parsed) = data_queue.get(timeout=0.1)
        except:
            continue

        logger.debug(f"Msg: {parsed}")
        if hasattr(parsed, "lat"):
            # Check for timeout since last high-precision update
            if time() - last_hppos > timeout:
                hppos = False

            # Update with high-precision data if valid
            if hasattr(parsed, "invalidLlh") and parsed.invalidLlh != 1:
                lat.append(parsed.lat)
                long.append(parsed.lon)
                height.append(parsed.height)
                hppos = True
                last_hppos = time()
            elif not hppos:  # Use less precise data after timeout
                lat.append(parsed.lat)
                long.append(parsed.lon)

        # Update DOP values if available
        if hasattr(parsed, "pDOP"):
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

        # Handle GGA messages
        if hasattr(parsed, "msgID") and parsed.msgID == "GGA":
            logger.info(f"Fix type: {parsed.quality}")
            fix.append(parsed.quality)
            gga_queue.put((raw_data, parsed))
        
        data_queue.task_done()

        # Ensure all deques have data before putting into gps_queue
        deq = [lat, long, height, fix, PDOP, HDOP, VDOP]
        
        count = 0
        for val in deq:
            if len(val) == 0:
                count = 1
        
        if count == 0:
            gps_queue.put((lat, long, height, fix, PDOP, HDOP, VDOP))
                
def ntrip(gga_queue: Queue, send_queue: Queue, kwargs):
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

def broadcast(tcp_server: TCPServer, gps_data_queue: Queue, ntrip_client: GNSSNTRIPClient, stop: Event):
    rate_count = 0
    last_count = time()
    while not stop.is_set():
        if not gps_data_queue.empty():
            connect = "ON" if ntrip_client.connected == True else "OFF"
            lat, long, height, fix, PDOP, HDOP, VDOP = gps_data_queue.get()
            lis = [lat, long, height, fix, PDOP, HDOP, VDOP]
            count = 0
            for val in lis:
                if len(val) == 0:
                    count = 1

            if count == 0:
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
                message = f"{lat.pop()},{long.pop()},{height.pop()},{fixtype},{PDOP.pop()},{HDOP.pop()},{VDOP.pop()},{connect}" + "\r\n"
                logger.info(f"Broadcasting to tcp clients: {message}")
                tcp_server.broadcast(message=message)
                rate_count += 1
                seconds = time() - last_count
                if seconds > 100:
                    last_count = time()
                    rate_count = 0
                logger.info(f"{rate_count/seconds} msg per sec")
                gps_data_queue.task_done()
            else:
                gps_data_queue.task_done()
            

def main(**kwargs):
    """
    Main routine.
    """

    port = kwargs.get("serport", "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00")
    baudrate = int(kwargs.get("baudrate", 57600))
    timeout = float(kwargs.get("timeout", 1))

    with Serial(port, baudrate, timeout=timeout) as serial_stream:
        ubxreader = UBXReader(
            serial_stream, 
            protfilter= UBX_PROTOCOL | NMEA_PROTOCOL)

        read_queue = Queue()
        gga_queue = Queue()
        gps_queue = Queue()
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

        logger.info("Starting ntrip thread...")
        ntrip_client = ntrip(gga_queue, send_queue, kwargs)

        logger.info("Starting broadcast thread...")
        broadcast_thread = Thread(
            target=broadcast,
            args=(
                tcp_server,
                gps_queue,
                ntrip_client,
                stop_event
            )
        )
        broadcast_thread.start()

        # loop until user presses Ctrl-C
        while not stop_event.is_set():
            try:
                # DO STUFF IN THE BACKGROUND...
                # Poll for each NMEA sentence type.
                # NB: Your receiver may not support all types. It will return a
                # GNTXT "NMEA unknown msg" response for any types it doesn't support.
                count = 0
                for nam in UBX_PAYLOADS_POLL:
                    # if nam[0:4] == "NAV-":
                    #     print(f"Polling {nam} message type...")
                    #     msg = UBXMessage("NAV", nam, POLL)
                    #     send_queue.put(msg)
                    #     count += 1
                    #     sleep(1)
                    if nam == "NAV-HPPOSLLH":
                        # logger.info(f"Polling {nam} message type...")
                        msg = UBXMessage("NAV", nam, POLL)
                        send_queue.put(msg)
                        count += 1
                        sleep(0.1)
                    if nam == "NAV-POSLLH":
                        # logger.info(f"Polling {nam} message type...")
                        msg = UBXMessage("NAV", nam, POLL)
                        send_queue.put(msg)
                        count += 1
                        sleep(0.1)
                    if nam == "NAV-DOP":
                        # logger.info(f"Polling {nam} message type...")
                        msg = UBXMessage("NAV", nam, POLL)
                        send_queue.put(msg)
                        count += 1
                        sleep(0.1)
                    if nam == "NAV-STATUS":
                        # logger.info(f"Polling {nam} message type...")
                        msg = UBXMessage("NAV", nam, POLL)
                        send_queue.put(msg)
                        count += 1
                        sleep(0.1)
                # sleep(1)
                # stop_event.set()

            except KeyboardInterrupt:  # capture Ctrl-C
                logger.info("\n\nTerminated by user.")
                stop_event.set()

        logger.info("\nStop signal set. Waiting for threads to complete...")
        io_thread.join()
        process_thread.join()
        logger.info("\nProcessing complete")


if __name__ == "__main__":

    main(**dict(arg.split("=") for arg in argv[1:]))