"""
ubxpoller.py

This example illustrates how to read, write and display UBX messages
"concurrently" using threads and queues. This represents a useful
generic pattern for many end user applications.

Usage:

python3 ubxpoller.py port="/dev/ttyACM0" baudrate=38400 timeout=0.1

It implements two threads which run concurrently:
1) an I/O thread which continuously reads UBX data from the
receiver and sends any queued outbound command or poll messages.
2) a process thread which processes parsed UBX data - in this example
it simply prints the parsed data to the terminal.
UBX data is passed between threads using queues.

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
from threading import Event, Thread
from time import sleep

from serial import Serial

from pyubx2 import (
    POLL_LAYER_BBR,
    POLL_LAYER_RAM,
    SET_LAYER_BBR,
    UBX_CONFIG_DATABASE,
    POLL, 
    UBX_PAYLOADS_POLL, 
    UBX_PROTOCOL, 
    UBXMessage, 
    UBXReader )

get_status = False

def io_data(
    stream: object,
    ubr: UBXReader,
    readqueue: Queue,
    sendqueue: Queue,
    stop: Event,
):
    """
    THREADED
    Read and parse inbound UBX data and place
    raw and parsed data on queue.

    Send any queued outbound messages to receiver.
    """
    # pylint: disable=broad-exception-caught

    while not stop.is_set():
        if stream.in_waiting:
            try:
                (raw_data, parsed_data) = ubr.read()
                if parsed_data:
                    readqueue.put((raw_data, parsed_data))

                # refine this if outbound message rates exceed inbound
                while not sendqueue.empty():
                    data = sendqueue.get(False)
                    if data is not None:
                        ubr.datastream.write(data.serialize())
                    sendqueue.task_done()

            except Exception as err:
                print(f"\n\nSomething went wrong {err}\n\n")
                continue


def process_data(queue: Queue, result: Queue, stop: Event):
    """
    THREADED
    Get UBX data from queue and display.
    """

    while not stop.is_set():
        if queue.empty() is False:
            (_, parsed) = queue.get()
            # TODO
            print(f"Parsed: {parsed}")
            result.put(parsed)
            queue.task_done()


def main(**kwargs):
    """
    Main routine.
    """

    port = kwargs.get("port", "/dev/ttyACM0")
    baudrate = int(kwargs.get("baudrate", 38400))
    timeout = float(kwargs.get("timeout", 0.1))

    with Serial(port, baudrate, timeout=timeout) as stream:
        ubxreader = UBXReader(stream, protfilter=UBX_PROTOCOL)

        read_queue = Queue()
        send_queue = Queue()
        result_queue =  Queue()
        stop_event = Event()
        stop_event.clear()

        io_thread = Thread(
            target=io_data,
            args=(
                stream,
                ubxreader,
                read_queue,
                send_queue,
                stop_event,
            ),
        )
        process_thread = Thread(
            target=process_data,
            args=(
                read_queue,
                result_queue,
                stop_event,
            ),
        )

        print("\nStarting handler threads. Press Ctrl-C to terminate...")
        io_thread.start()
        process_thread.start()

        list = []

        # loop until user presses Ctrl-C
        while not stop_event.is_set():
            try:
                # DO STUFF IN THE BACKGROUND...
                # poll all available NAV messages (receiver will only respond
                # to those NAV message types it supports; responses won't
                # necessarily arrive in sequence)
                count = 0
                msg = None
                # for name in UBX_CONFIG_DATABASE:
                #     position = 0
                #     layer = POLL_LAYER_RAM  # volatile memory
                #     CONFIG_KEY1 = "CFG_MSGOUT_UBX_MON_COMMS_UART1"
                #     CONFIG_VAL1 = 1
                #     CONFIG_KEY2 = "CFG_MSGOUT_UBX_MON_TXBUF_UART1"
                #     CONFIG_VAL2 = 1
                    
                #     keys = [CONFIG_KEY1, CONFIG_KEY2]
                #     keys.append(name)

                #     configs = [
                #         "CFG_NAVHPG_DGNSSMODE",
                #         "CFG_NAVSPG_DYNMODEL",
                #         "CFG_RATE_MEAS",
                #         "CFG_RTCM_DF003_IN",
                #         "CFG_RTCM_DF003_IN_FILTER",
                #         "CFG_UART1INPROT_RTCM3X",
                #         "CFG_UART1OUTPROT_RTCM3X",
                #         "CFG_UART2INPROT_RTCM3X",
                #         "CFG_UART2OUTPROT_RTCM3X"
                #     ]
                #     msg = UBXMessage.config_poll(layer, position, configs)
                #     print(f"Config: {name}")
                #     count += 1
                #     if count == 10:
                #         break
                #     sleep(1)
                position = 0
                layer = POLL_LAYER_RAM  # volatile memory
                configs = [
                        "CFG_NAVHPG_DGNSSMODE",
                        "CFG_NAVSPG_DYNMODEL",
                        "CFG_RATE_MEAS",
                        "CFG_RATE_NAV",
                        "CFG_RATE_TIMEREF",
                        "CFG_UART1INPROT_RTCM3X",
                        "CFG_UART2INPROT_RTCM3X",
                    ]
                for msgid in UBX_CONFIG_DATABASE:
                    if msgid[0:14] == "CFG_MSGOUT_NME":
                        if msgid[-4:] == "UART1" or msgid[-3:] == "USB":
                            if "GGA" in msgid or "GSV" in msgid or "GSA" in msgid:
                                configs.append(msgid)
                print(configs)
                print(len(configs))
                msg = UBXMessage.config_poll(layer, position, configs)
                send_queue.put(msg)
                print(f"Sending msgs: {configs}")
                while result_queue.empty:
                    sleep(1)
                    print("Waiting response...")
                stop_event.set()
                # print(f"{count} NAV message types polled.")

            except KeyboardInterrupt:  # capture Ctrl-C
                print("\n\nTerminated by user.")
                stop_event.set()

        print("\nStop signal set. Waiting for threads to complete...")
        io_thread.join()
        process_thread.join()
        print("\nProcessing complete")


if __name__ == "__main__":

    main(**dict(arg.split("=") for arg in argv[1:]))