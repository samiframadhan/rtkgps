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

from serial import Serial

from pynmeagps import NMEA_MSGIDS, POLL, NMEAMessage, NMEAReader
from pygnssutils import VERBOSITY_HIGH, GNSSNTRIPClient, set_logging

import socket
import base64
import time
from datetime import datetime, timedelta

class NTRIPClient:
    def _init_(self):
        # NTRIP server settings
        self.server = "69.64.185.41"
        self.port = 7801
        self.mountpoint = "MSM5"
        self.user = "grk28"
        self.password = "730d2"
        
        # Create socket
        self.socket = socket.create_connection((socket.gethostbyname(self.server), int(self.port)), timeout=0.1)
        
    def connect(self):
        try:
            # Connect to server
            print("connecting to server...")
            self.socket.connect((self.server, self.port))
            
            # Create HTTP request
            auth = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            request = (
                f"GET /{self.mountpoint} HTTP/1.1\r\n"
                f"Host: {self.server}:{self.port}\r\n"
                f"Ntrip-Version: Ntrip/2.0\r\n"
                f"User-Agent: NTRIP rtkconnect/1.0\r\n"
                f"Authorization: Basic {auth}\r\n"
                f"Connection: close\r\n"
                "\r\n"
            )
            
            print("Sending request...")
            # Send request
            self.socket.sendall(request.encode())
            
            # Check response
            response = self.socket.recv(4096).decode()
            if "ICY 200 OK" not in response:
                raise Exception(f"Server response error: {response}")
            
            print("Connected to NTRIP caster successfully")
            return True
            
        except Exception as e:
            print(f"Connection failed: {str(e)}")
            return False
    
    def send_gga(self, gga_string):
        try:
            # Add CRLF to GGA string
            gga_string = f"{gga_string}\r\n"
            self.socket.sendall(gga_string.encode())
            return True
        except Exception as e:
            print(f"Failed to send GGA: {str(e)}")
            return False
    
    def receive_rtcm(self):
        try:
            # Receive RTCM data (adjust buffer size as needed)
            data = self.socket.recv(1024)
            return data
        except Exception as e:
            print(f"Failed to receive RTCM: {str(e)}")
            return None
    
    def close(self):
        self.socket.shutdown(socket.SHUT_WR)
        self.socket.close()

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
                    nmr.datastream.write(data.serialize())
                sendqueue.task_done()

        except Exception as err:
            print(f"\n\nSomething went wrong {err}\n\n")
            continue


def process_data(gga_queue: Queue, data_queue: Queue, stop: Event):
    """
    THREADED
    Get NMEA data from data_queue and display.
    """

    while not stop.is_set():
        if data_queue.empty() is False:
            (raw_data, parsed) = data_queue.get()
            # print(parsed)
            if parsed.msgID == "GGA":
                fix = "3d" if parsed.quality == 1 else "2d"
                print(f"Fix : {parsed.quality}, Long :{parsed.lon}, Lat :{parsed.lat}")
                gga_queue.put(raw_data)
            data_queue.task_done()

def ntrip_correction(gga_queue : Queue, rtcm_correction_queue : Queue, stop: Event):
    gnc = GNSSNTRIPClient()
    gnc.run(
        server = "69.64.185.41",
        port = 7801,
        mountpoint = "MSM5",
        ntripuser = "grk28",
        ntrippassword = "730d2",
        gga_interval=30,
    )

    
    client = NTRIPClient()
    _last_gga = datetime.now()

    while not stop.is_set():

        if not client.connect():
            return
        else:
            data = client.receive_rtcm()
            rtcm_correction_queue.put(data)
            if datetime.now() > _last_gga + timedelta(seconds=2):
                if not gga_queue.empty():
                    gga_data = gga_queue.get()
                    client.send_gga(gga_string=gga_data)
                    gga_queue.task_done()
                else:
                    print("No GGA data received yet")

    print("Yeay")
    client.close()

def send_correction(stream : object, send : Queue, rtcm_correction_queue : Queue, stop : Event):
    while not stop.is_set():
        if not rtcm_correction_queue.empty:
            data = rtcm_correction_queue.get()
            stream.write(data)
            rtcm_correction_queue.task_done()

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
        rtcm_correction_queue = Queue()
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

        ntrip_thread = Thread(
            target=ntrip_correction,
            args=(
                gga_queue,
                rtcm_correction_queue,
                stop_event
            )
        )

        print("\nStarting handler threads. Press Ctrl-C to terminate...")
        io_thread.start()
        process_thread.start()
        ntrip_thread.start()

        # loop until user presses Ctrl-C
        while not stop_event.is_set():
            try:
                # DO STUFF IN THE BACKGROUND...
                # Poll for each NMEA sentence type.
                # NB: Your receiver may not support all types. It will return a
                # GNTXT "NMEA unknown msg" response for any types it doesn't support.
                for msgid in NMEA_MSGIDS:
                    print(
                        f"\nSending a GNQ message to poll for an {msgid} response...\n"
                    )
                    msg = NMEAMessage("EI", "GNQ", POLL, msgId=msgid)
                    send_queue.put(msg)
                    sleep(1)
                sleep(3)
                stop_event.set()

            except KeyboardInterrupt:  # capture Ctrl-C
                print("\n\nTerminated by user.")
                stop_event.set()

        print("\nStop signal set. Waiting for threads to complete...")
        io_thread.join()
        process_thread.join()
        ntrip_thread.join()
        print("\nProcessing complete")


if __name__ == "__main__":

    main(**dict(arg.split("=") for arg in argv[1:]))