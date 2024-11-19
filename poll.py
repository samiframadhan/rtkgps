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

from serial import Serial

from pynmeagps import NMEA_MSGIDS, POLL, NMEAMessage, NMEAReader
from pygnssutils import VERBOSITY_HIGH, VERBOSITY_DEBUG, set_logging

from ntripclient import GNSSNTRIPClient

logger = getLogger("rtkgps")
set_logging(getLogger("ntripclient"), VERBOSITY_DEBUG)
set_logging(getLogger("rtkgps"), VERBOSITY_DEBUG)
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


def process_data(gga_queue: Queue, data_queue: Queue, stop: Event):
    """
    THREADED
    Get NMEA data from data_queue and display.
    """

    while not stop.is_set():
        if data_queue.empty() is False:
            (raw_data, parsed) = data_queue.get()
            # logger.info(parsed)
            if hasattr(parsed, "lat") and hasattr(parsed, "lon"):
                logger.info(f"MSGID: {parsed.msgID}. Long :{parsed.lon}, Lat :{parsed.lat}")
                if hasattr(parsed, "alt"):
                    logger.info(f"Alt: {parsed.alt}")
                if hasattr(parsed, "quality"):
                    logger.info(f"Fix type: {parsed.quality}")
                if parsed.msgID == "GGA":
                    gga_queue.put((raw_data, parsed))
            data_queue.task_done()

def ntrip(gga_queue: Queue, send_queue: Queue, kwargs):
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
    )
    
    pass

def main(**kwargs):
    """
    Main routine.
    """

    port = kwargs.get("serport", "/dev/ttyS0")
    baudrate = int(kwargs.get("baudrate", 57600))
    timeout = float(kwargs.get("timeout", 1))

    with Serial(port, baudrate, timeout=timeout) as serial_stream:
        nmeareader = NMEAReader(serial_stream)

        read_queue = Queue()
        gga_queue = Queue()
        send_queue = Queue()
        stop_event = Event()

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
                stop_event,
            ),
        )

        ntrip(gga_queue, send_queue, kwargs)

        logger.info("\nStarting handler threads. Press Ctrl-C to terminate...")
        io_thread.start()
        process_thread.start()


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
                    logger.debug(f"NMEA Message sending...: {msg}")
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