# **RTK-GPS Service Documentation**

## **Overview**
This project interfaces a Raspberry Pi Zero W with a Zed-F9P RTK GPS module, leveraging Python code managed via a `systemd` service. It provides the following functionalities:
1. **TCP Server**: Broadcasts customized GPS data strings to connected clients.
2. **NTRIP Client**: Communicates with a Virtual Reference Station (VRS) NTRIP server.
3. **Zed-F9P Configuration and Polling**: Configures and polls data from the Zed-F9P GPS module.

---

## **Prerequisites**
1. **Hardware**:
   - Raspberry Pi Zero W.
   - Zed-F9P RTK GPS Module.
2. **Software**:
   - Python 3 (recommended to use a virtual environment).
   - Required Python modules:
     - `pyubx2`
     - `pygnssutils`
     - `ntripclient`
   - `udev` rules for device configuration.
   - `systemd` for service management.

---

## **Installation Steps**
### 1. **Clone the Repository**
Clone the project to your desired directory:
```bash
git clone https://github.com/samiframadhan/rtkgps /path/to/project
```

### 2. **Setup the Virtual Environment**
Navigate to the project directory and create a virtual environment:
```bash
cd /path/to/project
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. **Setup `udev` Rules**
Create a udev rule to assign a consistent device name for the Zed-F9P:
```bash
sudo nano /etc/udev/rules.d/99-f9p.rules
```
Add the following content:
```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1546", ATTRS{idProduct}=="01a9", SYMLINK+="f9pdev"
```
Reload udev rules:
```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

---

## **Systemd Service Configuration**

### 1. **Create the Service File**
Create a systemd service file:
```bash
sudo nano /etc/systemd/system/rtk.service
```

Add the following content:
```ini
[Unit]
Description=F9P RTK Service
After=network.target
Requires=f9pdev.device

[Service]
Environment=CWD=/path/to/project
ExecStartPre=/bin/sleep 2
ExecStart=${CWD}/.venv/bin/python3 ${CWD}/rtkgps/poll.py
ExecReload=${CWD}/.venv/bin/python3 ${CWD}/rtkgps/poll.py
RestartSec=2
Restart=always
KillMode=process
SyslogIdentifier=rtk-f9p
StandardOutput=null
StandardError=null

[Install]
WantedBy=default.target
```

Replace `/path/to/project` with the actual project directory path.

Similar file can be found on the systemd/ directory for reference.

### 2. **Reload Systemd and Enable the Service**
Reload the systemd daemon to register the new service:
```bash
sudo systemctl daemon-reload
```
Enable the service to start on boot:
```bash
sudo systemctl enable rtk.service
```

Start the service:
```bash
sudo systemctl start rtk.service
```

---

## **Usage**

### **Starting the Service**
```bash
sudo systemctl start rtk.service
```

### **Checking Service Status**
```bash
sudo systemctl status rtk.service
```

### **Stopping the Service**
```bash
sudo systemctl stop rtk.service
```

### **Reloading the Service**
If any changes are made to the code or configuration:
```bash
sudo systemctl reload rtk.service
```

---

## **Logs and Debugging**
### Separated logs
Log file is directed to the current project's directory called "logs"

### Access Logs
View the service logs using `less`:
```bash
less logs/rtk-<date>.logs
```

### Modify Log Level
To modify logging verbosity, adjust the `set_logging` levels in `poll.py`.

---

## **Project Features**

### 1. **Dynamic Configuration**
- Device name (`/dev/f9pdev`) dynamically assigned via `udev` rules.
- Systemd service dynamically adjusts paths using the `CWD` environment variable.

### 2. **Robust NTRIP Client**
- Reconnects automatically if the NTRIP connection is lost.

### 3. **TCP Server**
- Broadcasts GPS data in a structured format:
  ```
  Latitude,Longitude,Height,FixType,Satellites,PDOP,HDOP,VDOP,Heading,Speed,NTRIPStatus
  ```

### 4. **Failsafe Restart**
- Automatically restarts the service upon failure using systemd's `Restart=always`.

---

## **References**
- **Systemd Documentation**: [https://www.freedesktop.org/software/systemd/](https://www.freedesktop.org/software/systemd/)
- **PyUBX2 Library**: [https://github.com/semuconsulting/pyubx2](https://github.com/semuconsulting/pyubx2/)

---