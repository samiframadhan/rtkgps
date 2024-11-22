import socket
import threading
from logging import getLogger
from queue import Queue
from pynmeagps import set_logging

class TCPServer:
    def __init__(self, host='127.0.0.1', port=65432, stop= None):
        """
        Initializes the TCP server.
        :param host: Host address to bind the server to (default: localhost).
        :param port: Port number to listen on (default: 65432).
        """
        self.host = host
        self.port = port
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.clients = []
        self.is_running = False
        self.logger = getLogger(__name__)
        self.lock = threading.Lock()  # Lock for thread-safe client list access

    def start(self):
        """
        Starts the server and begins listening for connections.
        """
        try:
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)  # Allow up to 5 queued connections
            self.is_running = True
            self.logger.info(f"Server started on {self.host}:{self.port}")

            while self.is_running:
                client_socket, client_address = self.server_socket.accept()
                self.logger.info(f"Connection from {client_address}")

                # Add client to the list
                with self.lock:
                    self.clients.append(client_socket)

                # Handle the client in a separate thread
                client_thread = threading.Thread(
                    target=self.handle_client, args=(client_socket, client_address)
                )
                client_thread.daemon = True
                client_thread.start()
        except Exception as e:
            self.logger.info(f"Error: {e}")
        finally:
            self.stop()

    def handle_client(self, client_socket, client_address):
        """
        Handles communication with a single client.
        :param client_socket: Socket object for the connected client.
        :param client_address: Address of the connected client.
        """
        try:
            while True:
                data = client_socket.recv(1024)
                self.logger.info(f"Connected to: {data}")
                if not data:
                    break
                self.logger.info(f"Received from {client_address}: {data.decode('utf-8')}")
                
                # For this example, just echo back the received data
                response = f"Echo: {data.decode('utf-8')}"
                client_socket.sendall(response.encode('utf-8'))
        except Exception as e:
            self.logger.info(f"Error with client {client_address}: {e}")
        finally:
            # Remove client from the list
            with self.lock:
                self.clients.remove(client_socket)
            client_socket.close()
            self.logger.info(f"Client {client_address} disconnected.")

    def broadcast(self, message):
        """
        Sends a message to all connected clients.
        :param message: The message to send.
        """
        with self.lock:  # Ensure thread-safe access to the client list
            for client_socket in self.clients:
                try:
                    client_socket.sendall(message.encode('utf-8'))
                except Exception as e:
                    self.logger.info(f"Error sending to client: {e}")

    def stop(self):
        """
        Stops the server and closes all client connections.
        """
        self.logger.info("Shutting down the server.")
        self.is_running = False
        with self.lock:
            for client_socket in self.clients:
                try:
                    client_socket.close()
                except Exception as e:
                    self.logger.info(f"Error closing client socket: {e}")
            self.clients.clear()
        self.server_socket.close()