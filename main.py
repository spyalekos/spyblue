import os
import io
import re
import sys
import time
import queue
import socket
import select
import zipfile
import threading
import subprocess
import urllib.request
import numpy as np
import sounddevice as sd

# Unix/macOS terminal control libraries
import tty
import termios

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.progress import BarColumn, Progress

# Global constants
ADB_PATH = os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")
SNDCPY_PORT = 28200
APK_NAME = "com.rom1v.sndcpy"
APK_FILE = "sndcpy.apk"
APK_URL = "https://github.com/rom1v/sndcpy/releases/download/v1.1/sndcpy-v1.1.zip"
IP_FILE = ".device_ip"

SAMPLE_RATE = 48000
CHANNELS = 2
DTYPE = 'int16'
BUFFER_SIZE = 1024 * 4  # 4KB chunks
PREBUFFER_TIME = 0.2    # seconds
PREBUFFER_BYTES = int(SAMPLE_RATE * CHANNELS * 2 * PREBUFFER_TIME)

# Thread-safe stats and queues
audio_queue = queue.Queue(maxsize=500)
stop_event = threading.Event()
app_status = "Disconnected"
device_serial = "None"
data_rate = 0.0  # KB/s
volume_level = 0.0
buffer_fill = 0.0
total_bytes = 0
original_media_volume = None

console = Console()

class NonBlockingConsole:
    """Context manager for raw non-blocking terminal input on Unix/macOS."""
    def __init__(self):
        self.old_settings = None

    def __enter__(self):
        try:
            self.old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            pass
        return self

    def __exit__(self, type, value, traceback):
        if self.old_settings:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass

    def get_key(self):
        try:
            if select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], []):
                c = sys.stdin.read(1)
                if c == '\x1b':
                    # Ignore escape sequences (like Arrow Keys) which are multi-char
                    if select.select([sys.stdin], [], [], 0.05) == ([sys.stdin], [], []):
                        sys.stdin.read(2)
                        return None
                    return c
                return c
        except Exception:
            pass
        return None

def keyboard_listener_worker():
    """Background listener to intercept double Escape key press to exit."""
    global stop_event, app_status
    last_esc_time = 0
    with NonBlockingConsole() as nbc:
        while not stop_event.is_set():
            key = nbc.get_key()
            if key == '\x1b':
                now = time.time()
                if now - last_esc_time < 0.5:
                    app_status = "Terminating..."
                    stop_event.set()
                    break
                else:
                    last_esc_time = now
            elif key:
                # Any other key resets the double ESC timer
                last_esc_time = 0
            time.sleep(0.02)

def get_media_volume(adb_cmd, serial):
    """Retrieve the current music/media volume from the Android system settings."""
    try:
        result = subprocess.run(
            [adb_cmd, "-s", serial, "shell", "settings", "get", "system", "volume_music"],
            capture_output=True,
            text=True,
            check=True
        )
        val = result.stdout.strip()
        if val.isdigit():
            return int(val)
    except Exception:
        pass
    return 5

def set_media_volume(adb_cmd, serial, volume):
    """Programmatically set the music/media stream volume level on Android."""
    try:
        subprocess.run(
            [adb_cmd, "-s", serial, "shell", "cmd", "media_session", "volume", "--stream", "3", "--set", str(volume)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception:
        pass

def get_saved_ip():
    """Read the saved Android IP address from previous session."""
    if os.path.exists(IP_FILE):
        try:
            with open(IP_FILE, 'r') as f:
                ip = f.read().strip()
                if ip:
                    return ip
        except Exception:
            pass
    return None

def save_ip(ip):
    """Save the Android IP address to disk for auto-connection."""
    try:
        with open(IP_FILE, 'w') as f:
            f.write(ip)
    except Exception:
        pass

def get_device_ip(adb_cmd, serial):
    """Retrieve the wlan0 IP address of the device."""
    try:
        result = subprocess.run(
            [adb_cmd, "-s", serial, "shell", "ip", "addr", "show", "wlan0"],
            capture_output=True,
            text=True,
            check=True
        )
        match = re.search(r'inet\s+([\d\.]+)/', result.stdout)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None

def enable_wireless_adb(adb_cmd, serial):
    """Enable ADB over TCP/IP and connect wirelessly."""
    ip = get_device_ip(adb_cmd, serial)
    if not ip:
        return None
    
    save_ip(ip)
    
    try:
        # Switch ADB on device to tcpip port 5555
        subprocess.run([adb_cmd, "-s", serial, "tcpip", "5555"], check=True)
        time.sleep(2.0)
        
        # Connect to device IP
        subprocess.run([adb_cmd, "connect", f"{ip}:5555"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        
        # Verify it shows up in adb devices
        devices = check_devices(adb_cmd)
        if f"{ip}:5555" in devices:
            return f"{ip}:5555"
    except Exception:
        pass
    return None

def find_adb():
    """Detect adb path."""
    try:
        subprocess.run(["adb", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "adb"
    except FileNotFoundError:
        pass
    
    if os.path.exists(ADB_PATH):
        return ADB_PATH
    
    return None

def check_devices(adb_cmd):
    """Retrieve list of connected devices."""
    try:
        result = subprocess.run([adb_cmd, "devices"], capture_output=True, text=True, check=True)
        lines = result.stdout.strip().split('\n')
        devices = []
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices
    except Exception:
        return []

def download_apk():
    """Download the sndcpy release zip and extract the APK."""
    global app_status
    app_status = "Downloading APK..."
    try:
        req = urllib.request.Request(
            APK_URL, 
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
        )
        with urllib.request.urlopen(req) as response:
            zip_data = response.read()
        
        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
            apk_in_zip = next((name for name in z.namelist() if name.endswith("sndcpy.apk")), None)
            if apk_in_zip:
                with open(APK_FILE, 'wb') as f:
                    f.write(z.read(apk_in_zip))
                return True
    except Exception as e:
        app_status = f"Download Failed: {e}"
        time.sleep(3)
    return False

def setup_device(adb_cmd, serial):
    """Install the APK if missing and configure app permissions."""
    global app_status
    
    # Check if installed
    try:
        result = subprocess.run([adb_cmd, "-s", serial, "shell", "pm", "list", "packages", APK_NAME], capture_output=True, text=True)
        if f"package:{APK_NAME}" not in result.stdout:
            if not os.path.exists(APK_FILE):
                if not download_apk():
                    return False
            app_status = "Installing APK..."
            subprocess.run([adb_cmd, "-s", serial, "install", "-t", "-r", "-g", APK_FILE], check=True)
    except Exception as e:
        app_status = f"Install Error: {e}"
        time.sleep(3)
        return False

    # Grant media projection permissions
    app_status = "Granting Permissions..."
    try:
        subprocess.run([adb_cmd, "-s", serial, "shell", "appops", "set", APK_NAME, "PROJECT_MEDIA", "allow"], check=True)
        return True
    except Exception as e:
        app_status = f"Permission Error: {e}"
        time.sleep(3)
        return False

def run_audio_stream(adb_cmd, serial):
    """Start port forwarding and start the app on the device."""
    global app_status
    app_status = "Activating Stream..."
    
    # Remove existing forwarding if any, then add new one
    try:
        subprocess.run([adb_cmd, "-s", serial, "forward", "--remove", f"tcp:{SNDCPY_PORT}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
        
    try:
        subprocess.run([adb_cmd, "-s", serial, "forward", f"tcp:{SNDCPY_PORT}", "localabstract:sndcpy"], check=True)
    except Exception as e:
        app_status = f"Port Forward Error: {e}"
        time.sleep(3)
        return False

    try:
        subprocess.run([adb_cmd, "-s", serial, "shell", "am", "start", f"{APK_NAME}/.MainActivity"], check=True)
        app_status = "Waiting for Auth..."
        return True
    except Exception as e:
        app_status = f"App Start Error: {e}"
        time.sleep(3)
        return False

def socket_reader():
    """Reads raw audio data from the forwarded TCP socket."""
    global app_status, total_bytes, data_rate
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    connected = False
    retries = 15
    
    while not stop_event.is_set() and retries > 0:
        try:
            sock.connect(('127.0.0.1', SNDCPY_PORT))
            connected = True
            app_status = "Buffering..."
            break
        except socket.error:
            retries -= 1
            time.sleep(1)
            
    if not connected:
        app_status = "Socket Error"
        sock.close()
        stop_event.set()
        return

    last_time = time.time()
    bytes_since_last = 0
    
    while not stop_event.is_set():
        try:
            data = sock.recv(BUFFER_SIZE)
            if not data:
                app_status = "Finished"
                break
            
            # Put in queue, drop oldest if full to avoid building up latency
            try:
                audio_queue.put_nowait(data)
            except queue.Full:
                try:
                    audio_queue.get_nowait()
                    audio_queue.put_nowait(data)
                except queue.Empty:
                    pass
            
            # Update metrics
            data_len = len(data)
            total_bytes += data_len
            bytes_since_last += data_len
            
            current_time = time.time()
            dt = current_time - last_time
            if dt >= 1.0:
                data_rate = (bytes_since_last / 1024.0) / dt
                bytes_since_last = 0
                last_time = current_time
                
        except socket.error:
            app_status = "Socket Read Error"
            break
            
    sock.close()
    stop_event.set()

def audio_playback():
    """Plays audio data from the queue using sounddevice."""
    global app_status, volume_level, buffer_fill
    
    # Configure output stream
    stream = sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE
    )
    
    prebuffering = True
    prebuffer = bytearray()
    
    with stream:
        while not stop_event.is_set():
            # Calculate queue occupancy percentage
            buffer_fill = (audio_queue.qsize() / audio_queue.maxsize) * 100.0
            
            try:
                chunk = audio_queue.get(timeout=0.2)
                
                # Compute volume visualizer (RMS)
                audio_data = np.frombuffer(chunk, dtype=np.int16)
                if len(audio_data) > 0:
                    rms = np.sqrt(np.mean(audio_data.astype(np.float32)**2))
                    # Normalizing sound level (0.0 to 1.0)
                    volume_level = min(1.0, rms / 4000.0)
                
                if prebuffering:
                    prebuffer.extend(chunk)
                    if len(prebuffer) >= PREBUFFER_BYTES:
                        app_status = "Streaming"
                        stream.write(bytes(prebuffer))
                        prebuffer.clear()
                        prebuffering = False
                else:
                    stream.write(chunk)
                    
            except queue.Empty:
                if not prebuffering:
                    app_status = "Buffering..."
                    prebuffering = True
                volume_level = 0.0
                continue
            except Exception as e:
                app_status = f"Playback Error: {e}"
                break
                
    volume_level = 0.0
    buffer_fill = 0.0

def make_dashboard():
    """Generate the console UI dashboard using rich layout."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="main", size=10),
        Layout(name="footer", size=3)
    )
    
    # Header Panel
    header_text = Text("\nSpyBlue - Mac Bluetooth/USB Speaker Bridge", style="bold cyan", justify="center")
    layout["header"].update(Panel(header_text, border_style="cyan"))
    
    # Main Metrics Panel
    metrics_layout = Layout()
    metrics_layout.split_row(
        Layout(name="left"),
        Layout(name="right")
    )
    layout["main"].update(metrics_layout)
    
    # Left stats
    status_color = "green" if app_status == "Streaming" else "yellow" if "Error" not in app_status else "red"
    left_text = Text()
    left_text.append("Device Connection Status:\n\n", style="bold white")
    left_text.append(f"  Target Device: ", style="dim")
    left_text.append(f"{device_serial}\n", style="bold magenta")
    left_text.append(f"  Bridge Status: ", style="dim")
    left_text.append(f"{app_status}\n", style=f"bold {status_color}")
    left_text.append(f"  Data Rate:     ", style="dim")
    left_text.append(f"{data_rate:.1f} KB/s\n", style="bold green")
    left_text.append(f"  Data Received: ", style="dim")
    left_text.append(f"{total_bytes / (1024*1024):.2f} MB\n", style="bold blue")
    
    metrics_layout["left"].update(Panel(left_text, title="System Info", border_style="dim"))
    
    # Right Visualizers
    # Volume level progress bar
    vol_pct = int(volume_level * 100)
    vol_bar = "█" * (vol_pct // 5) + "░" * (20 - (vol_pct // 5))
    
    # Buffer level progress bar
    buf_bar = "█" * (int(buffer_fill) // 5) + "░" * (20 - (int(buffer_fill) // 5))
    
    right_text = Text()
    right_text.append("Audio & Buffer Monitoring:\n\n", style="bold white")
    right_text.append("  Volume Output Level:\n  ", style="dim")
    right_text.append(f"[{vol_bar}] {vol_pct}%\n\n", style="bold green" if vol_pct < 80 else "bold red")
    right_text.append("  Receiver Queue Buffer:\n  ", style="dim")
    right_text.append(f"[{buf_bar}] {buffer_fill:.1f}%\n", style="bold blue")
    
    metrics_layout["right"].update(Panel(right_text, title="Performance & Levels", border_style="dim"))
    
    # Footer Panel
    footer_text = Text("Press [Double ESC] (or Ctrl + C) to terminate the bridge and disconnect safely.", style="dim italic yellow", justify="center")
    layout["footer"].update(Panel(footer_text, border_style="yellow"))
    
    return layout

def main():
    global app_status, device_serial, stop_event, data_rate, total_bytes, original_media_volume
    
    adb_cmd = find_adb()
    if not adb_cmd:
        console.print("[red]Error: adb binary not found.[/red] Please verify you have Android Platform Tools installed.")
        console.print("Checked location: [dim]/Users/owner/Library/Android/sdk/platform-tools/adb[/dim]")
        sys.exit(1)
        
    console.clear()
    
    # Try connecting to previously saved IP at startup
    saved_ip = get_saved_ip()
    if saved_ip:
        app_status = f"Connecting to saved IP {saved_ip}..."
        try:
            subprocess.run([adb_cmd, "connect", f"{saved_ip}:5555"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    with Live(make_dashboard(), refresh_per_second=10, screen=True) as live:
        try:
            # Step 1: Device Detection Loop
            while not stop_event.is_set():
                app_status = "Searching for device..."
                devices = check_devices(adb_cmd)
                if devices:
                    # Prefer wireless connections over USB if both are present
                    wireless_devs = [d for d in devices if ":" in d]
                    if wireless_devs:
                        device_serial = wireless_devs[0]
                    else:
                        device_serial = devices[0]
                    break
                time.sleep(1.5)
                live.update(make_dashboard())
                
            if stop_event.is_set():
                return
                
            # Step 2: Setup Device App & Permissions
            if not setup_device(adb_cmd, device_serial):
                stop_event.set()
                
            # Auto-enable Wireless ADB if connected via USB
            if not stop_event.is_set() and ":" not in device_serial:
                app_status = "Enabling Wireless ADB..."
                live.update(make_dashboard())
                wireless_serial = enable_wireless_adb(adb_cmd, device_serial)
                if wireless_serial:
                    device_serial = wireless_serial
                    app_status = "Wireless Enabled! Unplug USB."
                    live.update(make_dashboard())
                    time.sleep(2.5)  # Give time for user to unplug
                else:
                    app_status = "Wireless failed. Using USB."
                    live.update(make_dashboard())
                    time.sleep(1.0)
                
            # Save original volume and set device volume to zero
            if not stop_event.is_set():
                original_media_volume = get_media_volume(adb_cmd, device_serial)
                set_media_volume(adb_cmd, device_serial, 0)
                
            # Step 3: Run audio stream forward and activate android side
            if not stop_event.is_set():
                if not run_audio_stream(adb_cmd, device_serial):
                    stop_event.set()
            
            # Step 4: Spawn audio, playback, and keyboard listener threads
            if not stop_event.is_set():
                reader_thread = threading.Thread(target=socket_reader, daemon=True)
                playback_thread = threading.Thread(target=audio_playback, daemon=True)
                keyboard_thread = threading.Thread(target=keyboard_listener_worker, daemon=True)
                
                reader_thread.start()
                playback_thread.start()
                keyboard_thread.start()
                
                # Main UI loop keeping screen updated
                while not stop_event.is_set():
                    live.update(make_dashboard())
                    time.sleep(0.1)
                    
                reader_thread.join(timeout=1.0)
                playback_thread.join(timeout=1.0)
                keyboard_thread.join(timeout=1.0)
                
        except KeyboardInterrupt:
            app_status = "Terminating..."
            stop_event.set()
            live.update(make_dashboard())
            time.sleep(0.5)
        finally:
            if device_serial != "None":
                # Restore original media volume if it was saved
                if original_media_volume is not None:
                    set_media_volume(adb_cmd, device_serial, original_media_volume)
                # Cleanup port forward
                try:
                    subprocess.run([adb_cmd, "-s", device_serial, "forward", "--remove", f"tcp:{SNDCPY_PORT}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                    
    console.print("\n[bold green]Bridge closed successfully.[/bold green] Adb port forwarding removed and phone volume restored.")

if __name__ == "__main__":
    main()
