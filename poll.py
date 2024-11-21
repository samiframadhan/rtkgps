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

from queue import Queue
from sys import argv
from os import getenv
from threading import Event, Thread
from time import sleep
from logging import getLogger
from collections import deque

from serial import Serial

from pynmeagps import NMEA_MSGIDS, POLL, NMEAMessage, NMEAReader
from pygnssutils import VERBOSITY_HIGH, VERBOSITY_DEBUG, set_logging

from ntripclient import GNSSNTRIPClient
from tcp import TCPServer

logger = getLogger("rtkgps")
# set_logging(getLogger("ntripclient"), VERBOSITY_HIGH)
set_logging(getLogger("rtkgps"), VERBOSITY_HIGH)
poll_str = ["GGA", "GLL", "GNS", "LR2", "MOB", "RMA", "RMB", "RMC", "TRF", "WPL", "BWC", "BWR"]
only_gga = ["GGA"]

def io_data(
    stream: object,
    nmr: NMEAReader,
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
                (raw_data, parsed_data) = nmr.read()
                
                if parsed_data:
                    readqueue.put((raw_data, parsed_data))

            if not sendqueue.empty():
                data = sendqueue.get(False)
                if data is not None:
                    if type(data) is tuple:
                        raw, parsed = data
                        nmr.datastream.write(raw)
                    else:
                        nmr.datastream.write(data.serialize())
                sendqueue.task_done()

        except Exception as err:
            logger.info(f"\n\nSomething went wrong {err}\n\n")
            continue


def process_data(gga_queue: Queue, data_queue: Queue, gps_queue: Queue, stop: Event):
    """
    THREADED
    Get NMEA data from data_queue and display.
    """

    lat = deque(maxlen=1)
    long = deque(maxlen=1)
    height = deque(maxlen=1)
    fix = deque(maxlen=1)
    PDOP = deque(maxlen=1)
    HDOP = deque(maxlen=1)
    VDOP = deque(maxlen=1)
    deq = [lat, long, height, fix, PDOP, HDOP, VDOP]

    while not stop.is_set():
        if data_queue.empty() is False:
            (raw_data, parsed) = data_queue.get()
            # logger.info(parsed)
            if hasattr(parsed, "lat") and hasattr(parsed, "lon"):
                logger.info(f"MSGID: {parsed.msgID}. Long :{parsed.lon}, Lat :{parsed.lat}")
                lat.append(parsed.lat)
                long.append(parsed.lon)
                if parsed.msgID == "GGA":
                    logger.info(f"Fix type: {parsed.quality}")
                    fix.append(parsed.quality)
                    height.append(parsed.alt)
                    gga_queue.put((raw_data, parsed))
            if parsed.msgID == "GSA":
                PDOP.append(parsed.PDOP)
                HDOP.append(parsed.HDOP)
                VDOP.append(parsed.VDOP)
            data_queue.task_done()
        count = 0
        lat.
        if len(lat) == 0:
            count = 1
        if len(long) == 0:
            count = 1
        if len(height) == 0:
            count = 1
        if len(fix) == 0:
            count = 1
        if len(PDOP) == 0:
            count = 1
        if len(HDOP) == 0:
            count = 1
        if len(VDOP) == 0:
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
                message = f"{lat.pop()},{long.pop()},{height.pop()},{fixtype},{PDOP.pop()},{HDOP.pop()},{VDOP.pop()},{connect}"
                logger.info(f"Broadcasting to tcp clients: {message}")
                tcp_server.broadcast(message=message)
                gps_data_queue.task_done()
            

def main(**kwargs):
    """
    Main routine.
    """

    port = kwargs.get("serport", "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00")
    baudrate = int(kwargs.get("baudrate", 57600))
    timeout = float(kwargs.get("timeout", 1))

    with Serial(port, baudrate, timeout=timeout) as serial_stream:
        nmeareader = NMEAReader(serial_stream)

        read_queue = Queue()
        gga_queue = Queue()
        gps_queue = Queue()
        send_queue = Queue()
        stop_event = Event()

        tcp_server = TCPServer(port=5051)

        server_thread = Thread(target=tcp_server.start)
        server_thread.daemon = True
        server_thread.start()

        io_thread = Thread(
            target=io_data,
            args=(
                serial_stream,
                nmeareader,
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
        io_thread.start()
        process_thread.start()

        ntrip_client = ntrip(gga_queue, send_queue, kwargs)

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
                for msgid in NMEA_MSGIDS:
                    # logger.info(
                    #     f"\nSending a GNQ message to poll for an {msgid} response...\n"
                    # )
                    msg = NMEAMessage("EI", "GNQ", POLL, msgId=msgid)
                    send_queue.put(msg)
                    sleep(1)
                sleep(2)
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