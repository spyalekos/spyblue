# SpyBlue - Android to Mac Audio Bridge

**SpyBlue** is a Python application that converts your Mac into a wireless speaker for your Android device. It leverages the **sndcpy** protocol to capture high-quality digital PCM audio (16-bit stereo, 48kHz) via ADB (Android Debug Bridge), working over both USB cable and entirely wirelessly over Wi-Fi.

---

## 🌟 Features

*   **Wireless Audio Streaming (Wi-Fi)**: After the initial connection via USB, the application automatically activates wireless debugging on the phone, allowing you to unplug the cable and listen to your music wirelessly.
*   **Auto-Reconnect**: Saves your device's IP address, so in subsequent runs, it connects automatically via Wi-Fi without requiring a USB cable.
*   **Automatic Phone Muting**: When the connection is established, the phone's speaker is automatically muted and the audio plays only through your Mac's speakers. Upon exit, the phone's media volume is restored to its original level.
*   **Modern Live Dashboard**: Displays real-time connection status, data transfer rate (KB/s), buffer queue health, and a live audio level visualizer.
*   **Double ESC Exit**: Press the **`Escape` key twice quickly** (or `Ctrl + C`) in the terminal to close the application and disconnect safely.

---

## 🛠️ Prerequisites

1.  **Android 10 or higher** on your device.
2.  **USB Debugging enabled** on your phone:
    *   Go to *Settings > About phone*.
    *   Tap *Build Number* 7 times to enable *Developer options*.
    *   Go to *Developer options* and turn on **USB Debugging**.
3.  Both Mac and Android devices must be connected to the **same Wi-Fi network** (for wireless operation).
4.  Android SDK Platform Tools (`adb`) installed on your Mac (automatically detected in the default path `~/Library/Android/sdk/platform-tools/adb`).

---

## 🚀 Usage Instructions

### 1. First Run (Via USB)

1.  Connect your phone to your Mac using a **USB cable**.
2.  Run the application from your terminal:
    ```bash
    ./spyblue
    ```
    *(Alternatively, if running from source code, use: `uv run main.py`)*
3.  **On Android**: A confirmation prompt will appear. Allow USB debugging from this computer.
4.  The application will automatically download `sndcpy.apk`, install it on your device, and request audio capture permission.
5.  **On Android**: Tap **Start now** on the popup cast/recording authorization prompt.
6.  Once you see **`Wireless Enabled! Unplug USB.`** on the terminal, you can **unplug the USB cable**. The audio stream will continue wirelessly!

---

### 2. Subsequent Runs (Completely Wireless)

As long as your phone and Mac are on the same Wi-Fi, you don't need a USB cable anymore:
1.  Open your terminal and run:
    ```bash
    ./spyblue
    ```
    *(Alternatively, if running from source code: `uv run main.py`)*
2.  The application will read the saved IP and connect automatically wirelessly.
3.  Tap **Start now** on your phone's screen when prompted.

---

## 🛑 Stopping the Application

Press the **`Escape` key twice quickly** (or `Ctrl + C`) in the terminal.
The application will:
1.  Stop the wireless transmission.
2.  Restore the media volume on your phone to the exact level it was before connecting.
3.  Safely release the network ports on the Mac.
