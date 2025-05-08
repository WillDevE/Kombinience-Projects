import soundfile as sf
import numpy as np
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import time
import sys
import sv_ttk  # Sun Valley theme for modern dark UI
import uuid
from scipy import signal
import json  # For settings persistence
import pyaudio
import wave
from collections import deque

class SoundboardApp:
    def __init__(self, root):
        """Initialize the application"""
        self.root = root
        self.root.title("Kombini Soundboard")
        self.root.minsize(800, 600)  # Set minimum size
        self.root.geometry("900x650")  # Set initial window size
        self.root.resizable(True, True)  # Allow resizing but we'll control min size

        # Apply the Sun Valley dark theme
        sv_ttk.set_theme("dark")

        # Settings file path
        self.settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

        # Set up initial variables with default values
        self.sample_rate = 44100     # Standard sample rate that's most compatible
        self.chunk_size = 2048       # Increased buffer size for better stability
        self.device_list = self.get_device_list()
        self.running = False
        self.pyaudio_instance = None
        self.stream = None
        self.volume = 0.7  # Initial volume (0.0 to 1.0)
        self.mic_volume = 0.8  # Initial mic volume (0.0 to 1.0)
        self.soundboard_volume = 0.8  # Initial soundboard volume (0.0 to 1.0)
        self.monitor_volume = 0.5  # Initial monitoring volume (0.0 to 1.0)
        self.self_listen = False  # Self-monitoring disabled by default
        self.soundboard_only = False  # Changed default to hear mic by default
        self.monitor_mic = False  # Don't monitor mic input by default in self-listen

        # Input/output device indices (to be loaded from settings)
        self.input_device_index = None
        self.output_device_index = None
        self.monitor_device_index = None

        # Direct playback state
        self.playing_sounds = []  # List of currently playing sounds with their positions
        self.sound_buffer_queue = deque(maxlen=64)  # Buffer for audio processing
        self.max_buffer_size = 4096  # Maximum expected buffer size for pre-allocation

        # Performance optimization flags
        self.use_premixing = True      # Mix audio in advance when possible
        self.use_resampling = False    # Whether to use resampling for performance
        self.use_limiter = True        # Whether to use the audio limiter to prevent clipping
        self.update_interval = 100     # UI update interval in ms (lower = more responsive, higher = less CPU)

        # Volume meters
        self.last_input_level = 0.0
        self.last_soundboard_level = 0.0
        self.meter_update_active = False

        self.sound_files = []
        self.filtered_sound_files = []  # For search functionality
        self.selected_sound_index = None
        self.current_position = 0  # For tracking playback position
        self.total_frames = 0      # Total frames in current audio
        self.progress_update_active = False
        self.dragging_progress = False  # For tracking progress bar drag

        # Sound buttons tracking
        self.sound_buttons = []
        self.last_played_button = None

        # Monitor output
        self.monitor_stream = None

        # Load settings if they exist
        self.load_settings()

        self.create_widgets()
        self.running = True  # Flag to control background processing

        # Auto-load sounds from the sounds directory
        self.auto_load_sounds()

        # Start streams automatically
        self.root.after(500, self.start_streams)  # Start after UI is fully loaded

        # Save settings when closing the app
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def get_device_list(self):
        """Get list of audio devices using PyAudio"""
        p = pyaudio.PyAudio()
        devices = []

        for i in range(p.get_device_count()):
            device_info = p.get_device_info_by_index(i)
            max_input_channels = device_info.get('maxInputChannels', 0)
            max_output_channels = device_info.get('maxOutputChannels', 0)

            if max_input_channels > 0 or max_output_channels > 0:
                devices.append((i, device_info['name']))

        p.terminate()
        return devices

    def audio_callback(self, in_data, frame_count, time_info, status):
        """PyAudio callback function for audio processing and mixing"""
        if status == pyaudio.paOutputUnderflow:
            print("Output underflow: audio buffer underrun", file=sys.stderr)
        elif status == pyaudio.paInputOverflow:
            print("Input overflow: audio buffer overflow", file=sys.stderr)

        # Convert PyAudio input data to numpy array for processing
        if in_data and self.input_device_index is not None:
            try:
                indata = np.frombuffer(in_data, dtype=np.float32)

                # Get the number of input channels
                if hasattr(self, 'pyaudio_instance') and self.input_device_index is not None:
                    input_info = self.pyaudio_instance.get_device_info_by_index(self.input_device_index)
                    input_channels = int(input_info.get('maxInputChannels', 2))
                    input_channels = min(2, max(1, input_channels))  # Ensure 1 or 2 channels
                else:
                    input_channels = 2  # Default to stereo

                # Reshape the input data based on actual input channels
                if input_channels == 2:
                    # Stereo input
                    indata = indata.reshape(-1, 2)
                else:
                    # Mono input, reshape then duplicate to stereo
                    indata = indata.reshape(-1, 1)
                    indata = np.column_stack([indata, indata])  # Convert mono to stereo

                # Ensure the reshaped data has correct frame count
                if len(indata) != frame_count:
                    # Adjust size if needed
                    if len(indata) > frame_count:
                        indata = indata[:frame_count]
                    else:
                        # Pad with zeros if too small
                        pad_frames = frame_count - len(indata)
                        indata = np.vstack([indata, np.zeros((pad_frames, 2), dtype=np.float32)])
            except Exception as e:
                print(f"Error processing input data: {e}", file=sys.stderr)
                indata = np.zeros((frame_count, 2), dtype=np.float32)
        else:
            # Create empty input data if no microphone
            indata = np.zeros((frame_count, 2), dtype=np.float32)

        # Initialize output buffer
        outdata = np.zeros((frame_count, 2), dtype=np.float32)

        # Process all currently playing sounds
        sounds_to_remove = []

        # Add any playing sounds to the output buffer
        for sound in self.playing_sounds:
            try:
                # Get current position and calculate remaining frames
                current_pos = sound['position']
                remaining = sound['total'] - current_pos

                # Determine how many frames to read
                frames_to_read = min(frame_count, remaining)

                if frames_to_read > 0:
                    # Get sound data slice
                    end_pos = min(current_pos + frames_to_read, sound['total'])
                    data_slice = sound['data'][current_pos:end_pos]

                    # Handle sample rate conversion if needed
                    file_rate = sound.get('sample_rate', self.sample_rate)
                    if file_rate != self.sample_rate:
                        # Calculate the ratio between sample rates
                        ratio = file_rate / self.sample_rate
                        if self.use_resampling:
                            # Use scipy's resampling for high quality
                            try:
                                data_slice = signal.resample(data_slice, int(len(data_slice) / ratio))
                            except Exception as e:
                                print(f"Resampling error: {e}", file=sys.stderr)
                        else:
                            # When resampling is disabled, we need to adjust playback speed
                            # Calculate how many samples to advance in the original data
                            # This prevents slow playback when resampling is off
                            adjusted_frames = int(frames_to_read * ratio)
                            if current_pos + adjusted_frames <= sound['total']:
                                end_pos = current_pos + adjusted_frames
                                data_slice = sound['data'][current_pos:end_pos]
                                # Now downsample using simple interpolation to match the needed frame count
                                if len(data_slice) > frames_to_read:
                                    indices = np.linspace(0, len(data_slice)-1, frames_to_read, dtype=int)
                                    data_slice = data_slice[indices]

                    # Ensure data matches frame count
                    actual_frames = len(data_slice)
                    if actual_frames < frames_to_read:
                        # Pad with zeros if needed
                        data_slice = np.pad(data_slice, (0, frames_to_read - actual_frames), 'constant')

                    # Reshape for stereo output if needed
                    if len(data_slice.shape) == 1:
                        # Convert mono to stereo
                        data_shaped = np.column_stack([data_slice] * 2)
                    else:
                        # Already multi-channel
                        data_shaped = data_slice

                    # Apply volume
                    data_shaped = data_shaped * (self.soundboard_volume * self.volume)

                    # Add to output
                    outdata[:frames_to_read] += data_shaped[:frames_to_read]

                    # Update position - Use actual frame advancement based on sample rate
                    if file_rate != self.sample_rate and not self.use_resampling:
                        # Adjust position advancement for different sample rates
                        sound['position'] += int(frames_to_read * ratio)
                    else:
                        # Normal position advancement for same sample rate or with resampling
                        sound['position'] += frames_to_read

                # Check if sound has finished playing
                if sound['position'] >= sound['total']:
                    sounds_to_remove.append(sound)

            except Exception as e:
                print(f"Error processing sound: {e}", file=sys.stderr)
                sounds_to_remove.append(sound)

        # Apply limiter if enabled
        if self.use_limiter:
            max_val = np.max(np.abs(outdata))
            if max_val > 0.95:
                scale = 0.95 / max_val
                outdata *= scale

        # Mix in microphone input if enabled (not soundboard_only)
        if not self.soundboard_only and self.input_device_index is not None:
            # Apply microphone volume - make a copy to avoid modifying original data
            mic_data = indata.copy() * self.mic_volume

            # Mix with soundboard output
            outdata += mic_data

            # Apply limiter again after adding microphone to prevent clipping
            if self.use_limiter:
                max_val = np.max(np.abs(outdata))
                if max_val > 0.95:
                    scale = 0.95 / max_val
                    outdata *= scale

        # Remove finished sounds
        for sound in sounds_to_remove:
            try:
                self.playing_sounds.remove(sound)

                # Reset highlighted button if needed
                if hasattr(self, 'last_played_button') and self.last_played_button:
                    for btn, idx in self.sound_buttons:
                        if idx == sound.get('index', -1):
                            current_style = btn.cget("style") or "TButton"
                            if "Active" in current_style:
                                base_style = current_style.replace("Active.", "")
                                btn.configure(style=base_style)
            except Exception as e:
                print(f"Error removing sound: {e}", file=sys.stderr)

        # If no more sounds playing, update UI
        if not self.playing_sounds and sounds_to_remove:
            # Update UI from the main thread
            self.root.after(0, lambda: self.status_label.config(text="Ready"))
            self.root.after(0, lambda: self.now_playing_label.config(text="None"))

        # Update meters
        if indata.size > 0:
            self.last_input_level = np.sqrt(np.mean(np.square(indata))) * 100
        if outdata.size > 0:
            self.last_soundboard_level = np.sqrt(np.mean(np.square(outdata))) * 100

        # Convert back to PyAudio format and return
        return (outdata.astype(np.float32).tobytes(), pyaudio.paContinue)

    def start_streams(self):
        """Start audio streams using PyAudio"""
        try:
            if self.stream is not None:
                self.stop_streams()

            # Get the selected input and output devices
            if not self.input_device_combo.current() >= 0:
                messagebox.showinfo("Input Required", "Please select an input device")
                return

            if not self.output_device_combo.current() >= 0:
                messagebox.showinfo("Output Required", "Please select an output device")
                return

            input_device_index = self.device_list[self.input_device_combo.current()][0]
            output_device_index = self.device_list[self.output_device_combo.current()][0]

            self.input_device_index = input_device_index
            self.output_device_index = output_device_index

            # Initialize PyAudio
            self.pyaudio_instance = pyaudio.PyAudio()

            # Configure the stream
            try:
                # Get device info
                input_device_info = self.pyaudio_instance.get_device_info_by_index(input_device_index)
                output_device_info = self.pyaudio_instance.get_device_info_by_index(output_device_index)

                # Get channels
                input_channels = min(2, input_device_info.get('maxInputChannels', 1))
                output_channels = min(2, output_device_info.get('maxOutputChannels', 2))

                # Set buffer size based on system capabilities
                buffer_size = self.chunk_size

                # Check if the device is VB-Cable
                is_vb_cable = "vb" in output_device_info.get('name', '').lower() or "cable" in output_device_info.get('name', '').lower()

                # Create the duplex stream
                self.stream = self.pyaudio_instance.open(
                    format=pyaudio.paFloat32,
                    channels=output_channels,
                    rate=self.sample_rate,
                    input=True,
                    output=True,
                    input_device_index=input_device_index,
                    output_device_index=output_device_index,
                    frames_per_buffer=buffer_size,
                    stream_callback=self.audio_callback,
                    start=False  # Start the stream manually
                )

                # Start the stream
                self.stream.start_stream()
                self.running = True

            except Exception as e:
                print(f"Error configuring stream: {e}", file=sys.stderr)
                # Try a fallback configuration
                try:
                    self.stream = self.pyaudio_instance.open(
                        format=pyaudio.paFloat32,
                        channels=2,
                        rate=44100,
                        input=True,
                        output=True,
                        input_device_index=input_device_index,
                        output_device_index=output_device_index,
                        frames_per_buffer=4096,
                        stream_callback=self.audio_callback,
                        start=False  # Start the stream manually
                    )

                    self.stream.start_stream()
                    self.running = True

                except Exception as fallback_error:
                    messagebox.showerror("Stream Error", f"Could not start audio stream: {fallback_error}")
                    self.stop_streams()
                    return

            # Configure monitoring if enabled
            if self.self_listen:
                self.setup_self_listen()

            # Start meter updates
            if not self.meter_update_active:
                self.meter_update_active = True
                self.update_meters()

            # Start progress bar updates
            if not hasattr(self, 'progress_update_active') or not self.progress_update_active:
                self.progress_update_active = True
                self.update_progress_bar()

            input_name = self.device_list[self.input_device_combo.current()][1]
            output_name = self.device_list[self.output_device_combo.current()][1]
            self.status_label.config(text=f"Stream started - {input_name} → {output_name}")

        except Exception as e:
            self.status_label.config(text=f"Error starting stream: {e}")
            print(f"Error: {e}", file=sys.stderr)
            messagebox.showerror("Stream Error", f"Could not start audio stream: {e}")

    def stop_streams(self):
        """Stop all audio streams"""
        try:
            self.running = False

            if hasattr(self, 'stream') and self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
                self.stream = None

            if hasattr(self, 'monitor_stream') and self.monitor_stream is not None:
                self.monitor_stream.stop_stream()
                self.monitor_stream.close()
                self.monitor_stream = None

            if hasattr(self, 'pyaudio_instance') and self.pyaudio_instance is not None:
                self.pyaudio_instance.terminate()
                self.pyaudio_instance = None

            # Stop any playing sounds
            self.stop_sound()

            # Reset meters
            self.mic_meter_var.set(0)
            self.sound_meter_var.set(0)
            self.progress_var.set(0)

            self.status_label.config(text="Stream stopped")

        except Exception as e:
            self.status_label.config(text=f"Error stopping stream: {e}")
            print(f"Error stopping stream: {e}", file=sys.stderr)

    def setup_self_listen(self):
        """Set up a monitoring stream for listening to the soundboard output"""
        if not self.self_listen or not self.monitor_device_combo.current() >= 0:
            return

        try:
            # Get the monitor device index
            monitor_device_index = self.device_list[self.monitor_device_combo.current()][0]

            # Close existing monitor stream if any
            if hasattr(self, 'monitor_stream') and self.monitor_stream is not None:
                self.monitor_stream.stop_stream()
                self.monitor_stream.close()
                self.monitor_stream = None

            # Get device info
            monitor_device_info = self.pyaudio_instance.get_device_info_by_index(monitor_device_index)

            # Only continue if it's an output device
            if monitor_device_info.get('maxOutputChannels', 0) <= 0:
                return

            # Store the monitor device index
            self.monitor_device_index = monitor_device_index

            # Create and start monitor stream
            self.monitor_stream = self.pyaudio_instance.open(
                format=pyaudio.paFloat32,
                channels=min(2, monitor_device_info.get('maxOutputChannels', 2)),
                rate=self.sample_rate,
                output=True,
                output_device_index=monitor_device_index,
                frames_per_buffer=self.chunk_size,
                stream_callback=self.monitor_callback,
                start=False  # Don't start immediately
            )

            # Start the monitor stream after configuration
            self.monitor_stream.start_stream()

        except Exception as e:
            print(f"Error setting up monitoring: {e}", file=sys.stderr)
            self.monitor_stream = None

    def monitor_callback(self, in_data, frame_count, time_info, status):
        """Callback for the monitoring stream"""
        try:
            # Create an empty buffer
            outdata = np.zeros((frame_count, 2), dtype=np.float32)

            # Monitor soundboard sounds when they're playing
            if self.playing_sounds: # Correct indentation here
                for sound in self.playing_sounds:
                    current_pos = sound.get('position', 0)
                    remaining = sound.get('total', 0) - current_pos

                    if remaining > 0:
                        # Get a chunk of the sound data
                        frames_to_read = min(frame_count, remaining)
                        end_pos = min(current_pos + frames_to_read, sound.get('total', 0))

                        if current_pos < len(sound.get('data', [])):
                            data_slice = sound['data'][current_pos:end_pos]

                            # Reshape for stereo if needed
                            if len(data_slice.shape) == 1:
                                data_shaped = np.column_stack([data_slice] * 2)
                            else:
                                data_shaped = data_slice

                            # Apply monitor volume
                            data_shaped = data_shaped * self.monitor_volume

                            # Add to output buffer
                            safe_frames = min(len(data_shaped), frame_count)
                            outdata[:safe_frames] += data_shaped[:safe_frames]

            # Add microphone input to monitor if enabled
            if self.monitor_mic and in_data and self.input_device_index is not None:
                try:
                    # Convert PyAudio input data to numpy array
                    indata = np.frombuffer(in_data, dtype=np.float32)

                    # Reshape based on input channels
                    if hasattr(self, 'pyaudio_instance') and self.input_device_index is not None:
                        input_info = self.pyaudio_instance.get_device_info_by_index(self.input_device_index)
                        input_channels = int(input_info.get('maxInputChannels', 2))
                        input_channels = min(2, max(1, input_channels))  # Ensure 1 or 2 channels
                    else:
                        input_channels = 2  # Default to stereo

                    # Reshape and convert mono to stereo if needed
                    if input_channels == 2:
                        indata = indata.reshape(-1, 2)
                    else:
                        indata = indata.reshape(-1, 1)
                        indata = np.column_stack([indata, indata])  # Convert mono to stereo

                    # Apply monitor volume to mic input
                    mic_monitor = indata * (self.monitor_volume * self.mic_volume)

                    # Add to output buffer
                    frames_to_use = min(len(mic_monitor), frame_count)
                    outdata[:frames_to_use] += mic_monitor[:frames_to_use]
                except Exception as e:
                    print(f"Error monitoring microphone: {e}", file=sys.stderr)

            # Apply limiting if needed
            if self.use_limiter:
                max_val = np.max(np.abs(outdata))
                if max_val > 0.95:
                    scale = 0.95 / max_val
                    outdata *= scale

            return (outdata.astype(np.float32).tobytes(), pyaudio.paContinue)

        except Exception as e:
            print(f"Error in monitor callback: {e}", file=sys.stderr)
            # Return silence on error
            return (np.zeros((frame_count, 2), dtype=np.float32).tobytes(), pyaudio.paContinue)

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Create tabbed interface
        self.tabs = ttk.Notebook(main_frame)
        self.tabs.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=1)

        # Create tabs
        self.settings_tab = ttk.Frame(self.tabs)
        self.soundboard_tab = ttk.Frame(self.tabs)

        self.tabs.add(self.soundboard_tab, text="Soundboard")
        self.tabs.add(self.settings_tab, text="Audio Settings")

        # Configure both tabs to expand properly
        for tab in [self.settings_tab, self.soundboard_tab]:
            tab.columnconfigure(0, weight=1)
            tab.rowconfigure(0, weight=1)

        # Create settings UI in settings tab
        self.create_settings_ui()

        # Create soundboard UI in soundboard tab
        self.create_soundboard_ui()

        # Add click handlers to all scales for proper positioning
        self.bind_scale_click_handlers()

    def bind_scale_click_handlers(self):
        """Add click handlers to all ttk.Scale widgets for better click positioning"""
        # This method is now a no-op as we want to disable click-to-position
        pass

    def scale_click(self, event):
        """This method has been disabled to prevent click-to-position functionality"""
        # Don't do anything when user clicks on scale widgets
        pass

    def create_settings_ui(self):
        """Create the settings UI on the settings tab"""
        # Settings panel - Device selection and controls
        settings_frame = ttk.Frame(self.settings_tab, padding="10")
        settings_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        settings_frame.columnconfigure(0, weight=1)

        # Create styles for volume meters
        style = ttk.Style()
        style.configure("Mic.Horizontal.TProgressbar",
                       background="green",
                       troughcolor="#2d2d2d")
        style.configure("Playback.Horizontal.TProgressbar",
                       background="#4a9cea",
                       troughcolor="#2d2d2d")
        style.configure("Sound.Horizontal.TProgressbar",
                       background="#28a745",
                       troughcolor="#2d2d2d")
        style.configure("Accent.TButton",
                       background="#4a9cea",
                       foreground="white")
        style.configure("Success.TButton",
                       background="#28a745",
                       foreground="white")
        style.configure("Danger.TButton",
                       background="#dc3545",
                       foreground="white")
        style.configure("Active.TButton",
                       background="#ffa500",
                       foreground="black")

        # --- Input Device Selection ---
        input_frame = ttk.LabelFrame(settings_frame, text="Input Device (Your Real Microphone)", padding="5")
        input_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)

        self.input_device_var = tk.StringVar()
        self.input_device_combo = ttk.Combobox(input_frame, textvariable=self.input_device_var,
                                              values=[d[1] for d in self.device_list],
                                              state="readonly", width=30)
        self.input_device_combo.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        self.input_device_combo.bind("<<ComboboxSelected>>", self.restart_streams)

        # Auto-select VB-Cable or microphone input
        self.auto_select_input()

        # --- Mic Volume Control ---
        mic_volume_frame = ttk.Frame(input_frame)
        mic_volume_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        ttk.Label(mic_volume_frame, text="Mic Volume:").grid(row=0, column=0, sticky=tk.W)
        self.mic_volume_scale = ttk.Scale(mic_volume_frame, from_=0.0, to=1.0, orient=tk.HORIZONTAL,
                                       command=self.set_mic_volume, value=self.mic_volume)
        self.mic_volume_scale.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5)
        mic_volume_frame.columnconfigure(1, weight=1)

        # --- Mic Level Meter ---
        mic_meter_frame = ttk.Frame(input_frame)
        mic_meter_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        ttk.Label(mic_meter_frame, text="Mic Level:").grid(row=0, column=0, sticky=tk.W)

        # Create the mic level meter
        self.mic_meter_var = tk.DoubleVar(value=0.0)
        self.mic_meter = ttk.Progressbar(mic_meter_frame, orient="horizontal",
                                     length=200, mode="determinate",
                                     variable=self.mic_meter_var,
                                     style="Mic.Horizontal.TProgressbar")
        self.mic_meter.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5)
        mic_meter_frame.columnconfigure(1, weight=1)

        # --- Output Device Selection ---
        output_frame = ttk.LabelFrame(settings_frame, text="Output Device (VB-Cable Input)", padding="5")
        output_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)

        self.output_device_var = tk.StringVar()
        self.output_device_combo = ttk.Combobox(output_frame, textvariable=self.output_device_var,
                                               values=[d[1] for d in self.device_list],
                                               state="readonly", width=30)
        self.output_device_combo.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        self.output_device_combo.bind("<<ComboboxSelected>>", self.restart_streams)

        # Auto-select VB-Cable output
        self.auto_select_vb_output()

        # --- Output Volume Control (replacement for master volume) ---
        output_volume_frame = ttk.Frame(output_frame)
        output_volume_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        ttk.Label(output_volume_frame, text="Output Volume:").grid(row=0, column=0, sticky=tk.W)
        self.volume_scale = ttk.Scale(output_volume_frame, from_=0.0, to=1.0, orient=tk.HORIZONTAL,
                                     command=self.set_volume, value=self.volume)
        self.volume_scale.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5)
        output_volume_frame.columnconfigure(1, weight=1)

        # --- Monitor Device Selection ---
        monitor_frame = ttk.LabelFrame(settings_frame, text="Self-Listen (Hear Soundboard)", padding="5")
        monitor_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)

        # Enable/disable monitoring
        self.monitor_var = tk.BooleanVar(value=self.self_listen)
        monitor_check = ttk.Checkbutton(monitor_frame, text="Enable Self-Listen",
                                      variable=self.monitor_var,
                                      command=self.toggle_self_listen)
        monitor_check.grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)

        # Enable/disable microphone in monitor
        self.monitor_mic_var = tk.BooleanVar(value=self.monitor_mic)
        monitor_mic_check = ttk.Checkbutton(monitor_frame, text="Include Microphone in Self-Listen",
                                           variable=self.monitor_mic_var,
                                           command=self.toggle_monitor_mic)
        monitor_mic_check.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)

        # Add tooltip for monitor mic option
        self.create_tooltip(monitor_mic_check,
                          "When enabled, you will hear both your microphone and soundboard.\n"
                          "When disabled, you will only hear the soundboard sounds.")

        # Monitor device selection
        monitor_device_frame = ttk.Frame(monitor_frame)
        monitor_device_frame.grid(row=1, column=0, columnspan=2, sticky=(tk.W, tk.E), padx=5, pady=5)
        ttk.Label(monitor_device_frame, text="Output Device:").grid(row=0, column=0, sticky=tk.W)

        self.monitor_device_var = tk.StringVar()
        self.monitor_device_combo = ttk.Combobox(monitor_device_frame, textvariable=self.monitor_device_var,
                                               values=[d[1] for d in self.device_list],
                                               state="readonly", width=30)
        self.monitor_device_combo.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5, pady=5)
        self.monitor_device_combo.bind("<<ComboboxSelected>>", self.restart_monitor)

        # Auto-select a monitoring device (usually speakers/headphones)
        self.auto_select_monitor()

        # Monitor volume control
        monitor_volume_frame = ttk.Frame(monitor_frame)
        monitor_volume_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        ttk.Label(monitor_volume_frame, text="Monitor Volume:").grid(row=0, column=0, sticky=tk.W)
        self.monitor_volume_scale = ttk.Scale(monitor_volume_frame, from_=0.0, to=1.0, orient=tk.HORIZONTAL,
                                           command=self.set_monitor_volume, value=self.monitor_volume)
        self.monitor_volume_scale.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5)
        monitor_volume_frame.columnconfigure(1, weight=1)

        # --- Advanced Options ---
        advanced_frame = ttk.LabelFrame(settings_frame, text="Advanced Options", padding="5")
        advanced_frame.grid(row=3, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)

        # Enable/disable resampling
        self.resampling_var = tk.BooleanVar(value=self.use_resampling)
        resampling_check = ttk.Checkbutton(advanced_frame, text="Enable Audio Resampling (try if sound is slowed down)",
                                         variable=self.resampling_var,
                                         command=self.toggle_resampling)
        resampling_check.grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)

        # Add tooltip explaining resampling
        self.create_tooltip(resampling_check,
                          "Enables automatic sample rate conversion when needed.\n"
                          "Try this if audio playback sounds slowed down or too fast.\n"
                          "Disable if it causes crackling or other audio issues.")

        # Enable/disable audio limiter
        self.limiter_var = tk.BooleanVar(value=self.use_limiter)
        limiter_check = ttk.Checkbutton(advanced_frame, text="Enable Audio Limiter (prevents distortion when multiple sounds play)",
                                       variable=self.limiter_var,
                                       command=self.toggle_limiter)
        limiter_check.grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)

        # Add tooltip explaining the limiter
        self.create_tooltip(limiter_check,
                          "Prevents audio distortion when multiple sounds play simultaneously.\n"
                          "Recommended for most users. Disable only if you experience audio quality issues.")

        # Enable/disable microphone input
        self.mic_input_var = tk.BooleanVar(value=not self.soundboard_only)
        mic_input_check = ttk.Checkbutton(advanced_frame, text="Enable Microphone Input",
                                          variable=self.mic_input_var,
                                          command=self.toggle_mic_input)
        mic_input_check.grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)

        # --- Status Display in a Fixed Box ---
        status_frame = ttk.LabelFrame(settings_frame, text="Status", padding="5")
        status_frame.grid(row=4, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)

        # Create a fixed size frame to contain the status label
        status_container = ttk.Frame(status_frame, width=250, height=40)
        status_container.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        status_container.pack_propagate(False)  # Prevents children from affecting frame size

        # Fixed width status label with wrapping and scrolling
        self.status_label = ttk.Label(status_container, text="Initializing...",
                                    wraplength=240, anchor="w", justify="left")
        self.status_label.pack(fill="both", expand=True)
        status_frame.columnconfigure(0, weight=1)

    def toggle_resampling(self):
        """Toggle audio resampling option"""
        self.use_resampling = self.resampling_var.get()
        self.save_settings()

        # Update status to reflect the change
        if self.use_resampling:
            self.status_label.config(text="Audio resampling enabled")
        else:
            self.status_label.config(text="Audio resampling disabled")

        # If we're currently running, restart streams to apply the change
        if self.running and self.stream is not None:
            self.restart_streams()

    def toggle_limiter(self):
        """Toggle audio limiter option"""
        self.use_limiter = self.limiter_var.get()
        self.save_settings()

        # Update status to reflect the change
        if self.use_limiter:
            self.status_label.config(text="Audio limiter enabled")
        else:
            self.status_label.config(text="Audio limiter disabled")

    def toggle_mic_input(self):
        """Toggle microphone input option"""
        self.soundboard_only = not self.mic_input_var.get()
        self.save_settings()

        # Update status to reflect the change
        if self.soundboard_only:
            self.status_label.config(text="Microphone input disabled")
        else:
            self.status_label.config(text="Microphone input enabled")

    def auto_select_input(self):
        # First try to use saved setting if available
        if hasattr(self, 'saved_input_device') and self.saved_input_device:
            for i, (idx, name) in enumerate(self.device_list):
                if name == self.saved_input_device:
                    self.input_device_combo.current(i)
                    print(f"Using saved input device: {name}")
                    return

        # Try to auto-select a real microphone (not VB-Cable)
        for i, (idx, name) in enumerate(self.device_list):
            if ("mic" in name.lower() or "microphone" in name.lower()) and "vb" not in name.lower() and "cable" not in name.lower():
                self.input_device_combo.current(i)
                return

        # If no specific microphone found, select any input device
        for i, (idx, name) in enumerate(self.device_list):
            if "input" in name.lower() and "vb" not in name.lower() and "cable" not in name.lower():
                self.input_device_combo.current(i)
                return

        # If nothing found, select the first device
        if self.device_list:
            self.input_device_combo.current(0)

    def auto_select_vb_output(self):
        # First try to use saved setting if available
        if hasattr(self, 'saved_output_device') and self.saved_output_device:
            for i, (idx, name) in enumerate(self.device_list):
                if name == self.saved_output_device:
                    self.output_device_combo.current(i)
                    print(f"Using saved output device: {name}")
                    return

        # Try to select VB-Cable Input as output device
        for i, (idx, name) in enumerate(self.device_list):
            if ("vb" in name.lower() or "cable" in name.lower()) and "input" in name.lower():
                self.output_device_combo.current(i)
                return

        # If VB-Cable not found, select any output device
        for i, (idx, name) in enumerate(self.device_list):
            if "speakers" in name.lower() or "headphones" in name.lower():
                self.output_device_combo.current(i)
                messagebox.showinfo("VB-Cable Not Found",
                                   "VB-Cable doesn't appear to be installed. For best results, please install VB-Cable and restart the application.")
                return

        # If nothing found, select the first output device
        if self.device_list:
            self.output_device_combo.current(0)

    def format_time(self, seconds):
        """Format seconds into MM:SS format"""
        minutes = int(seconds // 60)
        seconds = int(seconds % 60)
        return f"{minutes}:{seconds:02d}"

    def update_progress_bar(self):
        """Update the progress bar for sound playback"""
        if self.running and self.total_frames > 0:
            # Calculate progress percentage
            progress = min(100, int((self.current_position / self.total_frames) * 100))
            self.progress_var.set(progress)

            # Update time labels - but only when playing
            if self.playing_sounds:
                current_time = self.current_position / self.sample_rate
                total_time = self.total_frames / self.sample_rate
                self.current_time_label.config(text=self.format_time(current_time))
                self.total_time_label.config(text=self.format_time(total_time))

            # Schedule the next update - use configurable update interval for efficiency
            self.root.after(self.update_interval, self.update_progress_bar)
        else:
            if not self.running:
                self.progress_update_active = False
            else:
                # Schedule the next update (still running but no sound playing)
                self.root.after(self.update_interval, self.update_progress_bar)

    def load_sounds(self):
        """Load sound files from a directory"""
        directory = filedialog.askdirectory(title="Select Sound Directory")
        if not directory:
            return

        # Clear existing sounds if any
        self.sound_files = []

        try:
            # Find all audio files in the directory
            audio_extensions = ('.wav', '.mp3', '.ogg', '.flac')
            sound_paths = []
            for root, _, files in os.walk(directory):
                for file in files:
                    if file.lower().endswith(audio_extensions):
                        sound_paths.append(os.path.join(root, file))

            # Check if we found any sounds
            if not sound_paths:
                messagebox.showinfo("No Sounds Found",
                                  f"No audio files found in {directory}.\nSupported formats: {', '.join(audio_extensions)}")
                return

            # Show a progress dialog
            progress_dialog = tk.Toplevel(self.root)
            progress_dialog.title("Loading Sounds")
            progress_dialog.geometry("300x100")
            progress_dialog.transient(self.root)
            progress_dialog.grab_set()

            ttk.Label(progress_dialog, text="Loading sound files...").pack(pady=10)

            progress_var = tk.DoubleVar()
            progress_bar = ttk.Progressbar(progress_dialog, variable=progress_var, length=250)
            progress_bar.pack(pady=10)

            # Load each sound file
            for i, path in enumerate(sound_paths):
                try:
                    # Update progress
                    progress_var.set((i / len(sound_paths)) * 100)
                    progress_dialog.update()

                    # Load the sound file
                    sound_data, sample_rate = sf.read(path, dtype='float32')

                    # Convert stereo to mono if needed for memory efficiency
                    if len(sound_data.shape) > 1 and sound_data.shape[1] > 1:
                        sound_data = np.mean(sound_data, axis=1)

                    # Optionally resample for performance if sample rate differs significantly
                    if self.use_resampling and sample_rate != self.sample_rate and abs(sample_rate - self.sample_rate) > 100:
                        # Resample to target sample rate
                        print(f"Resampling {os.path.basename(path)} from {sample_rate}Hz to {self.sample_rate}Hz")
                        sound_data = signal.resample(sound_data, int(len(sound_data) * self.sample_rate / sample_rate))
                        sample_rate = self.sample_rate

                    # Get additional sound info
                    sound_info = {
                        'sample_rate': sample_rate,
                        'channels': 1,  # We convert to mono
                        'duration': len(sound_data) / sample_rate
                    }

                    # Store the sound data
                    self.sound_files.append((path, sound_data, sound_info))

                except Exception as e:
                    print(f"Error loading {path}: {e}", file=sys.stderr)

            # Close progress dialog
            progress_dialog.destroy()

            # Update sound list in UI
            if hasattr(self, 'update_sound_list'):
                self.update_sound_list()

            # Update status
            self.status_label.config(text=f"Loaded {len(self.sound_files)} sound files")

            # Select the first sound if available
            if self.sound_files:
                self.selected_sound_index = 0

            # Initialize filtered_sound_files with all sounds
            self.filtered_sound_files = list(range(len(self.sound_files)))

            # Restore saved buttons if available
            self.restore_saved_buttons()

        except Exception as e:
            messagebox.showerror("Error", f"Error loading sound files: {e}")
            print(f"Error: {e}", file=sys.stderr)

    def select_sound(self, event=None):
        selected_indices = self.sound_listbox.curselection()
        if selected_indices:
            listbox_index = selected_indices[0]

            # Get the original index from the filtered list
            if 0 <= listbox_index < len(self.filtered_sound_files):
                orig_index = self.filtered_sound_files[listbox_index]
                self.selected_sound_index = orig_index

                filename = os.path.basename(self.sound_files[orig_index][0])
                self.status_label.config(text=f"Selected: {filename}")

                # Update total time for the selected file
                _, data, _ = self.sound_files[orig_index]
                self.total_frames = data.shape[0]
                total_time = self.total_frames / self.sample_rate
                self.total_time_label.config(text=self.format_time(total_time))
                self.current_time_label.config(text="0:00")
                self.progress_var.set(0)

    def play_sound(self, event=None):
        """Play the selected sound effect and update UI"""
        if self.selected_sound_index is None or self.selected_sound_index >= len(self.sound_files):
            return

        try:
            # Use the preloaded sound data
            sound_path, sound_data, sound_info = self.sound_files[self.selected_sound_index]
            filename = os.path.basename(sound_path)

            # Update progress bar total
            self.total_frames = len(sound_data)
            self.current_position = 0

            # Generate a unique ID for this sound
            sound_id = str(uuid.uuid4())

            # Add sound to playing sounds list with additional optimization properties
            self.playing_sounds.append({
                'id': sound_id,
                'data': sound_data.copy(),  # Make a copy to avoid modifying original data
                'position': 0,
                'total': len(sound_data),
                'index': self.selected_sound_index,  # Store original index for seeking
                'sample_rate': sound_info['sample_rate'],
                'needs_resampling': abs(sound_info['sample_rate'] - self.sample_rate) > 100,
                'premixed_data': None  # Will be filled during callback if premixing is enabled
            })

            # Update UI
            self.now_playing_label.config(text=filename)

            # Highlight the button
            if hasattr(self, 'last_played_button') and self.last_played_button:
                # Restore original style - the button might have a custom style
                for btn, idx in self.sound_buttons:
                    if btn == self.last_played_button:
                        # Get the button's original style from its configuration
                        original_style = btn.cget("style") or "TButton"
                        # If it's currently highlighted, restore the original style
                        if "Active" in original_style:
                            base_style = original_style.replace("Active.", "")
                            btn.configure(style=base_style)

            # Find and highlight the button for this sound
            for btn, idx in self.sound_buttons:
                if idx == self.selected_sound_index:
                    current_style = btn.cget("style") or "TButton"
                    # Only modify if not already highlighted
                    if "Active" not in current_style:
                        btn.configure(style="Active." + current_style)
                    self.last_played_button = btn
                    break

            # Update status
            self.status_label.config(text=f"Playing: {filename}")

        except Exception as e:
            self.status_label.config(text=f"Error playing sound: {e}")
            print(f"Error: {e}", file=sys.stderr)

    def stop_sound(self):
        """Stop all currently playing sounds"""
        self.playing_sounds = []

        # Reset any highlighted buttons
        if hasattr(self, 'sound_buttons'):
            for btn, _ in self.sound_buttons:
                current_style = btn.cget("style") or "TButton"
                if "Active" in current_style:
                    base_style = current_style.replace("Active.", "")
                    btn.configure(style=base_style)

        # Reset UI
        self.now_playing_label.config(text="None")
        self.progress_var.set(0)
        self.current_time_label.config(text="0:00")
        self.sound_meter_var.set(0)

        # Update status
        self.status_label.config(text="Playback stopped")

    def set_volume(self, value):
        self.volume = float(value)
        # Save settings after change
        self.save_settings()

    def set_mic_volume(self, value):
        self.mic_volume = float(value)
        # Save settings after change
        self.save_settings()

    def set_soundboard_volume(self, value):
        self.soundboard_volume = float(value)
        # Save settings after change
        self.save_settings()

    def set_monitor_volume(self, value):
        self.monitor_volume = float(value)
        # Save settings after change
        self.save_settings()

    def load_settings(self):
        """Load settings from JSON file"""
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    settings = json.load(f)

                # Load volume settings
                if 'volume' in settings:
                    self.volume = settings['volume']
                if 'mic_volume' in settings:
                    self.mic_volume = settings['mic_volume']
                if 'soundboard_volume' in settings:
                    self.soundboard_volume = settings['soundboard_volume']
                if 'monitor_volume' in settings:
                    self.monitor_volume = settings['monitor_volume']
                if 'self_listen' in settings:
                    self.self_listen = settings['self_listen']
                if 'soundboard_only' in settings:
                    self.soundboard_only = settings['soundboard_only']
                if 'monitor_mic' in settings:
                    self.monitor_mic = settings['monitor_mic']

                # Load advanced settings
                if 'use_resampling' in settings:
                    self.use_resampling = settings['use_resampling']
                if 'use_limiter' in settings:
                    self.use_limiter = settings['use_limiter']

                # Load device selections - we'll store the names and find matching indices
                self.saved_input_device = settings.get('input_device', None)
                self.saved_output_device = settings.get('output_device', None)
                self.saved_monitor_device = settings.get('monitor_device', None)

                # Load saved button data if available
                self.saved_buttons = settings.get('buttons', [])

                print(f"Settings loaded from {self.settings_file}")
        except Exception as e:
            print(f"Error loading settings: {e}", file=sys.stderr)

    def save_settings(self):
        """Save current settings to JSON file"""
        try:
            # Prevent saving settings too frequently
            current_time = time.time()
            if hasattr(self, '_last_save_time') and current_time - self._last_save_time < 1.0:
                return
            self._last_save_time = current_time

            # Get current device names
            input_device_name = None
            output_device_name = None
            monitor_device_name = None

            if hasattr(self, 'input_device_combo') and self.input_device_combo.current() >= 0:
                input_device_idx = self.input_device_combo.current()
                if 0 <= input_device_idx < len(self.device_list):
                    input_device_name = self.device_list[input_device_idx][1]

            if hasattr(self, 'output_device_combo') and self.output_device_combo.current() >= 0:
                output_device_idx = self.output_device_combo.current()
                if 0 <= output_device_idx < len(self.device_list):
                    output_device_name = self.device_list[output_device_idx][1]

            if hasattr(self, 'monitor_device_combo') and self.monitor_device_combo.current() >= 0:
                monitor_device_idx = self.monitor_device_combo.current()
                if 0 <= monitor_device_idx < len(self.device_list):
                    monitor_device_name = self.device_list[monitor_device_idx][1]

            # Save sound buttons data
            button_data = []
            for button, sound_index in self.sound_buttons:
                # Get the button's label and style
                label = button.cget("text")
                style = button.cget("style") or "TButton"

                # Map ttk style back to our style names
                style_name = "Default"
                if "Accent" in style:
                    style_name = "Accent"
                elif "Success" in style:
                    style_name = "Success"
                elif "Danger" in style:
                    style_name = "Danger"

                # Only save if sound index is valid
                if 0 <= sound_index < len(self.sound_files):
                    # Save sound filename instead of index (more robust)
                    sound_path = self.sound_files[sound_index][0]
                    sound_filename = os.path.basename(sound_path)

                    button_info = {
                        "label": label,
                        "style": style_name,
                        "sound_filename": sound_filename
                    }
                    button_data.append(button_info)

            # Create settings dict
            settings = {
                'volume': self.volume,
                'mic_volume': self.mic_volume,
                'soundboard_volume': self.soundboard_volume,
                'monitor_volume': self.monitor_volume,
                'self_listen': self.self_listen,
                'use_resampling': self.use_resampling,
                'use_limiter': self.use_limiter,
                'soundboard_only': self.soundboard_only,
                'monitor_mic': self.monitor_mic,
                'input_device': input_device_name,
                'output_device': output_device_name,
                'monitor_device': monitor_device_name,
                'buttons': button_data
            }

            # Save to file
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=4)

            print(f"Settings saved to {self.settings_file}")
        except Exception as e:
            print(f"Error saving settings: {e}", file=sys.stderr)

    def create_soundboard_ui(self):
        """Create the soundboard UI with customizable sound buttons"""
        soundboard_panel = ttk.Frame(self.soundboard_tab, padding="10")
        soundboard_panel.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        soundboard_panel.columnconfigure(0, weight=1)
        soundboard_panel.rowconfigure(1, weight=1)  # Make the button grid expandable

        # Control panel at the top
        control_panel = ttk.Frame(soundboard_panel)
        control_panel.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        control_panel.columnconfigure(4, weight=1)  # Make last column expandable

        # Add buttons for sound management
        load_btn = ttk.Button(control_panel, text="Load Sounds", command=self.load_sounds, style="Accent.TButton")
        load_btn.grid(row=0, column=0, padx=5, pady=5)

        # Add a button to create a new sound button
        create_btn = ttk.Button(control_panel, text="Create Button", command=self.create_sound_button)
        create_btn.grid(row=0, column=1, padx=5, pady=5)

        # Button to show sound library
        library_btn = ttk.Button(control_panel, text="Sound Library", command=self.show_sound_library)
        library_btn.grid(row=0, column=2, padx=5, pady=5)

        # Stop all button
        stop_all_btn = ttk.Button(control_panel, text="Stop All", command=self.stop_sound, style="Accent.TButton")
        stop_all_btn.grid(row=0, column=3, padx=5, pady=5)

        # Now playing indicator
        ttk.Label(control_panel, text="Now Playing:").grid(row=0, column=4, padx=(20,5), pady=5, sticky=tk.E)
        self.now_playing_label = ttk.Label(control_panel, text="None", font=("", 9, "italic"))
        self.now_playing_label.grid(row=0, column=5, padx=5, pady=5, sticky=tk.W)

        # Button frame for sound buttons - scrollable
        button_frame_container = ttk.Frame(soundboard_panel)
        button_frame_container.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        button_frame_container.columnconfigure(0, weight=1)
        button_frame_container.rowconfigure(0, weight=1)

        # Canvas for scrolling
        self.button_canvas = tk.Canvas(button_frame_container, borderwidth=0, background="#1E1E1E", height=300, width=600)
        self.button_canvas.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Set up scrollbar for the button grid
        button_scroll = ttk.Scrollbar(button_frame_container, orient="vertical", command=self.button_canvas.yview)
        button_scroll.grid(row=0, column=1, sticky=(tk.N, tk.S))
        self.button_canvas.configure(yscrollcommand=button_scroll.set)

        # Frame for the sound buttons
        self.button_frame = ttk.Frame(self.button_canvas)
        self.button_canvas.create_window((0, 0), window=self.button_frame, anchor="nw", tags="self.button_frame")
        self.button_frame.bind("<Configure>", self.on_button_frame_configure)

        # Configure columns for button grid
        for i in range(4):  # 4 buttons per row
            self.button_frame.columnconfigure(i, weight=1)

        # Progress bar for currently playing sound
        progress_frame = ttk.LabelFrame(soundboard_panel, text="Playback Control", padding="5")
        progress_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        progress_frame.columnconfigure(0, weight=1)

        # Progress display
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(progress_frame, orient="horizontal",
                                          length=200, mode="determinate",
                                          variable=self.progress_var,
                                          style="Playback.Horizontal.TProgressbar")
        self.progress_bar.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)

        # Add bindings for seeking
        self.progress_bar.bind("<Button-1>", self.progress_click)
        self.progress_bar.bind("<B1-Motion>", self.progress_drag)
        self.progress_bar.bind("<ButtonRelease-1>", self.progress_release)

        # Time display
        time_frame = ttk.Frame(progress_frame)
        time_frame.grid(row=1, column=0, sticky=(tk.W, tk.E))
        time_frame.columnconfigure(0, weight=1)
        time_frame.columnconfigure(1, weight=1)

        self.current_time_label = ttk.Label(time_frame, text="0:00")
        self.current_time_label.grid(row=0, column=0, sticky=tk.W)

        self.total_time_label = ttk.Label(time_frame, text="0:00")
        self.total_time_label.grid(row=0, column=1, sticky=tk.E)

        # Sound Level Meter
        sound_meter_frame = ttk.Frame(progress_frame)
        sound_meter_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        sound_meter_frame.columnconfigure(1, weight=1)

        ttk.Label(sound_meter_frame, text="Sound Level:").grid(row=0, column=0, sticky=tk.W)

        # Create the sound level meter
        self.sound_meter_var = tk.DoubleVar(value=0.0)
        self.sound_meter = ttk.Progressbar(sound_meter_frame, orient="horizontal",
                                        length=200, mode="determinate",
                                        variable=self.sound_meter_var,
                                        style="Sound.Horizontal.TProgressbar")
        self.sound_meter.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5)

        # Volume Control at the bottom
        volume_frame = ttk.Frame(progress_frame)
        volume_frame.grid(row=3, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        volume_frame.columnconfigure(1, weight=1)

        ttk.Label(volume_frame, text="Soundboard Volume:").grid(row=0, column=0, sticky=tk.W)
        self.soundboard_volume_scale = ttk.Scale(volume_frame, from_=0.0, to=1.0, orient=tk.HORIZONTAL,
                                             command=self.set_soundboard_volume, value=self.soundboard_volume)
        self.soundboard_volume_scale.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5)

        # Add a tooltip for the progress bar
        self.create_tooltip(self.progress_bar, "Click or drag to seek in the audio")

        # Create a search frame and filtered sound files list for compatibility
        self.filtered_sound_files = []
        self.search_var = tk.StringVar()
        self.search_var.trace("w", self.filter_sounds)

        # Initialize the custom button list
        self.sound_buttons = []

    def on_button_frame_configure(self, event):
        """Adjust the canvas scroll region when the button frame changes size"""
        self.button_canvas.configure(scrollregion=self.button_canvas.bbox("all"))

    def create_sound_button(self):
        """Create a new customizable sound button"""
        if not self.sound_files:
            messagebox.showinfo("No Sounds", "Please load some sounds first.")
            self.load_sounds()
            return

        # Create a dialog for button configuration
        dialog = tk.Toplevel(self.root)
        dialog.title("Create Sound Button")
        dialog.geometry("400x300")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        # Create a frame for the dialog content
        dialog_frame = ttk.Frame(dialog, padding="10")
        dialog_frame.pack(fill="both", expand=True)

        # Button name
        ttk.Label(dialog_frame, text="Button Label:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        name_var = tk.StringVar()
        name_entry = ttk.Entry(dialog_frame, textvariable=name_var, width=30)
        name_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5, pady=5)

        # Sound selection
        ttk.Label(dialog_frame, text="Select Sound:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)

        # Create a listbox with scrollbar
        sound_frame = ttk.Frame(dialog_frame)
        sound_frame.grid(row=1, column=1, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        sound_frame.columnconfigure(0, weight=1)
        sound_frame.rowconfigure(0, weight=1)

        sound_listbox = tk.Listbox(sound_frame, selectmode=tk.SINGLE, height=10,
                                 background="#2d2d2d", foreground="white",
                                 selectbackground="#4a9cea", selectforeground="white")
        sound_listbox.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        scrollbar = ttk.Scrollbar(sound_frame, orient="vertical", command=sound_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))
        sound_listbox['yscrollcommand'] = scrollbar.set

        # Fill the listbox with sound names
        for filepath, _, _ in self.sound_files:
            sound_listbox.insert(tk.END, os.path.basename(filepath))

        # Button color (theme)
        ttk.Label(dialog_frame, text="Button Style:").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        style_var = tk.StringVar(value="Default")
        style_combo = ttk.Combobox(dialog_frame, textvariable=style_var,
                                  values=["Default", "Accent", "Success", "Danger"],
                                  state="readonly")
        style_combo.grid(row=2, column=1, sticky=(tk.W, tk.E), padx=5, pady=5)

        # Command buttons
        button_frame = ttk.Frame(dialog_frame)
        button_frame.grid(row=3, column=0, columnspan=2, sticky=(tk.E), padx=5, pady=15)

        def on_cancel():
            dialog.destroy()

        def on_create():
            # Get values
            button_label = name_var.get().strip()
            if not button_label:
                button_label = "Sound Button"

            selection = sound_listbox.curselection()
            if not selection:
                messagebox.showinfo("Selection Required", "Please select a sound.")
                return

            sound_index = selection[0]
            button_style = style_var.get()

            # Add the button to the grid
            self.add_sound_button(button_label, sound_index, button_style)
            dialog.destroy()

        cancel_btn = ttk.Button(button_frame, text="Cancel", command=on_cancel)
        cancel_btn.pack(side="right", padx=5)

        create_btn = ttk.Button(button_frame, text="Create", command=on_create, style="Accent.TButton")
        create_btn.pack(side="right", padx=5)

        # Focus the name entry
        name_entry.focus_set()

    def add_sound_button(self, label, sound_index, button_style="Default"):
        """Add a sound button to the button grid"""
        # Calculate grid position (4 buttons per row)
        num_buttons = len(self.sound_buttons)
        row = num_buttons // 4
        col = num_buttons % 4

        # Create a frame for the button to allow better styling
        btn_frame = ttk.Frame(self.button_frame, padding=5)
        btn_frame.grid(row=row, column=col, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)

        # Map style name to ttk style
        style_map = {
            "Default": "TButton",
            "Accent": "Accent.TButton",
            "Success": "Success.TButton",
            "Danger": "Danger.TButton"
        }
        ttk_style = style_map.get(button_style, "TButton")

        # Create the button
        button = ttk.Button(btn_frame, text=label, style=ttk_style,
                          command=lambda idx=sound_index: self.play_specific_sound(idx))
        button.pack(fill="both", expand=True, padx=2, pady=2)

        # Store the button reference
        self.sound_buttons.append((button, sound_index))

    def play_specific_sound(self, sound_index):
        """Play a specific sound from its index"""
        if 0 <= sound_index < len(self.sound_files):
            self.selected_sound_index = sound_index
            self.play_sound()

    def show_sound_library(self):
        """Show the sound library dialog with all loaded sounds"""
        if not self.sound_files:
            messagebox.showinfo("No Sounds", "Please load some sounds first.")
            self.load_sounds()
            return

        # Create a dialog for the library
        self._sound_library_dialog = tk.Toplevel(self.root)
        dialog = self._sound_library_dialog
        dialog.title("Sound Library")
        dialog.geometry("500x500")
        dialog.transient(self.root)
        dialog.grab_set()

        # Create a frame for the dialog content
        dialog_frame = ttk.Frame(dialog, padding="10")
        dialog_frame.pack(fill="both", expand=True)
        dialog_frame.columnconfigure(0, weight=1)
        dialog_frame.rowconfigure(1, weight=1)

        # Search bar
        search_frame = ttk.Frame(dialog_frame)
        search_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        search_frame.columnconfigure(1, weight=1)

        ttk.Label(search_frame, text="Search:").grid(row=0, column=0, sticky=tk.W, padx=5)
        search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=search_var)
        search_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5)

        # Sound list
        list_frame = ttk.Frame(dialog_frame)
        list_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self._library_listbox = tk.Listbox(list_frame, selectmode=tk.SINGLE,
                                       background="#2d2d2d", foreground="white",
                                       selectbackground="#4a9cea", selectforeground="white",
                                       borderwidth=0, highlightthickness=0)
        sound_listbox = self._library_listbox
        sound_listbox.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Add scrollbar to listbox
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=sound_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))
        sound_listbox['yscrollcommand'] = scrollbar.set

        # Fill the listbox with sound names (from filtered sounds)
        self._update_sound_library_list()

        # Button frame
        btn_frame = ttk.Frame(dialog_frame)
        btn_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)

        def play_selected():
            selection = sound_listbox.curselection()
            if selection:
                idx = selection[0]
                # Get original index from filtered list
                if 0 <= idx < len(self.filtered_sound_files):
                    orig_idx = self.filtered_sound_files[idx]
                    self.selected_sound_index = orig_idx
                    self.play_sound()

        def create_button_from_selected():
            selection = sound_listbox.curselection()
            if selection:
                idx = selection[0]
                # Get original index from filtered list
                if 0 <= idx < len(self.filtered_sound_files):
                    orig_idx = self.filtered_sound_files[idx]
                    filename = os.path.basename(self.sound_files[orig_idx][0])
                    # Use filename without extension as button label
                    label = os.path.splitext(filename)[0]
                    self.add_sound_button(label, orig_idx)
                    messagebox.showinfo("Button Created", f"Button for '{label}' created successfully.")

        play_btn = ttk.Button(btn_frame, text="Play", command=play_selected, style="Accent.TButton")
        play_btn.grid(row=0, column=0, padx=5, pady=5)

        create_btn = ttk.Button(btn_frame, text="Create Button", command=create_button_from_selected)
        create_btn.grid(row=0, column=1, padx=5, pady=5)

        close_btn = ttk.Button(btn_frame, text="Close", command=dialog.destroy)
        close_btn.grid(row=0, column=2, padx=5, pady=5)

        # Implement search function
        def filter_sounds_library(*args):
            search_text = search_var.get().lower()

            # Update main application search var to filter sounds
            self.search_var.set(search_text)

            # Update the listbox
            self._update_sound_library_list()

        # Connect search function to the search entry
        search_var.trace("w", filter_sounds_library)

        # Focus the search entry
        search_entry.focus_set()

    def _update_sound_library_list(self):
        """Update the sound library listbox with filtered sounds"""
        if not hasattr(self, '_library_listbox'):
            return

        # Clear the listbox
        self._library_listbox.delete(0, tk.END)

        # Add filtered sounds
        for idx in self.filtered_sound_files:
            if 0 <= idx < len(self.sound_files):
                filepath = self.sound_files[idx][0]
                filename = os.path.basename(filepath)
                self._library_listbox.insert(tk.END, filename)


    def update_meters(self):
        """Update the volume level meters"""
        if self.running:
            # Calculate levels from playing sounds
            sound_level = 0.0

            if self.playing_sounds:
                # Limit processing to just the first sound for performance
                sound = self.playing_sounds[0]
                if 'position' in sound and 'data' in sound:
                    pos = sound['position']
                    # Use a smaller sample for level calculation, and only calculate every other update
                    if hasattr(self, '_meter_update_counter'):
                        self._meter_update_counter += 1
                    else:
                        self._meter_update_counter = 0

                    if self._meter_update_counter % 2 == 0:  # Only update every other time
                        chunk_start = max(0, pos - 256)  # Smaller window for efficiency
                        chunk_end = min(len(sound['data']), pos + 256)
                        if chunk_end > chunk_start:
                            chunk = sound['data'][chunk_start:chunk_end]
                            # Calculate RMS level
                            level = np.sqrt(np.mean(np.square(chunk))) * 100
                            sound_level = min(100, level)  # Scale to 0-100

                        # Store the level to avoid recalculating
                        self.last_soundboard_level = sound_level
                    else:
                        # Use the cached level
                        sound_level = self.last_soundboard_level

            # Update the progress bars
            mic_level = 0  # We don't have real mic input in this version
            self.mic_meter_var.set(mic_level)
            self.sound_meter_var.set(sound_level)

            # Schedule the next update - use a configurable interval
            self.root.after(self.update_interval, self.update_meters)
        else:
            self.meter_update_active = False

    def progress_click(self, event):
        """This method has been disabled to prevent click-to-position functionality"""
        # Don't do anything when user clicks on the progress bar
        pass

    def progress_drag(self, event):
        """Handle drag on the progress bar for seeking"""
        if not self.playing_sounds:
            return

        # Calculate the percentage of the drag position with padding adjustment
        widget = event.widget
        width = widget.winfo_width()

        # Similar padding adjustment as in progress_click
        padding = 4
        usable_width = width - (2 * padding)

        # Calculate relative position (0.0 to 1.0) accounting for padding
        if event.x < padding:
            click_position = 0.0
        elif event.x > width - padding:
            click_position = 1.0
        else:
            click_position = (event.x - padding) / usable_width

        # Ensure the position is within bounds
        click_position = max(0.0, min(1.0, click_position))

        # Update the progress var temporarily
        self.progress_var.set(click_position * 100)

        # Update time display based on drag position
        sound = self.playing_sounds[0]
        temp_position = int(click_position * sound['total'])
        seconds = temp_position / self.sample_rate
        minutes = int(seconds // 60)
        seconds = int(seconds % 60)
        self.current_time_label.config(text=f"{minutes}:{seconds:02d}")

    def progress_release(self, event):
        """Handle release of mouse button on progress bar to finalize seeking"""
        if not self.playing_sounds:
            return

        # Calculate the percentage of the release position with padding adjustment
        widget = event.widget
        width = widget.winfo_width()

        # Similar padding adjustment as in progress_click
        padding = 4
        usable_width = width - (2 * padding)

        # Calculate relative position (0.0 to 1.0) accounting for padding
        if event.x < padding:
            release_position = 0.0
        elif event.x > width - padding:
            release_position = 1.0
        else:
            release_position = (event.x - padding) / usable_width

        # Ensure the position is within bounds
        release_position = max(0.0, min(1.0, release_position))

        # Get the currently playing sound
        sound = self.playing_sounds[0]

        # Calculate the new position in frames
        new_position = int(release_position * sound['total'])

        # Update the position
        sound['position'] = new_position
        self.current_position = new_position
        self.progress_var.set(release_position * 100)

        # Update time display
        self.update_time_display()

    def update_time_display(self):
        """Update the time display for the current playing sound"""
        if not self.playing_sounds:
            return

        # Get the currently playing sound
        sound = self.playing_sounds[0]

        # Calculate current and total time
        current_seconds = sound['position'] / self.sample_rate
        total_seconds = sound['total'] / self.sample_rate

        # Format as MM:SS
        current_minutes = int(current_seconds // 60)
        current_seconds = int(current_seconds % 60)

        total_minutes = int(total_seconds // 60)
        total_seconds = int(total_seconds % 60)

        # Update labels
        self.current_time_label.config(text=f"{current_minutes}:{current_seconds:02d}")
        self.total_time_label.config(text=f"{total_minutes}:{total_seconds:02d}")

    def update_progress(self):
        """Update the progress bar for the current playing sound"""
        if not self.playing_sounds:
            self.progress_var.set(0)
            self.current_time_label.config(text="0:00")
            self.total_time_label.config(text="0:00")
            self.now_playing_label.config(text="None")
            self.sound_meter_var.set(0)
            return

        # Get the currently playing sound
        sound = self.playing_sounds[0]

        # Update progress percentage
        progress_percentage = (sound['position'] / sound['total']) * 100
        self.progress_var.set(progress_percentage)

        # Update time display
        self.update_time_display()

        # Update sound level meter (simulated here, would be real audio levels in practice)
        if sound['position'] < sound['total']:
            # Get a chunk of audio data around the current position
            chunk_start = max(0, sound['position'] - 1024)
            chunk_end = min(sound['total'], sound['position'] + 1024)
            chunk = sound['data'][chunk_start:chunk_end]

            if len(chunk) > 0:
                # Calculate RMS amplitude as a simple level indicator
                level = np.sqrt(np.mean(np.square(chunk)))
                normalized_level = min(100, level * 100)  # Scale to 0-100
                self.sound_meter_var.set(normalized_level)

    def create_tooltip(self, widget, text):
        """Create a tooltip for a widget"""
        def enter(event):
            x, y, _, _ = widget.bbox("insert")
            x += widget.winfo_rootx() + 25
            y += widget.winfo_rooty() + 25

            # Create a toplevel window
            self.tooltip = tk.Toplevel(widget)
            self.tooltip.wm_overrideredirect(True)
            self.tooltip.wm_geometry(f"+{x}+{y}")

            label = ttk.Label(self.tooltip, text=text, justify=tk.LEFT,
                           background="#2d2d2d", foreground="white",
                           relief=tk.SOLID, borderwidth=1, padding=5)
            label.pack(ipadx=1)

        def leave(event):
            if hasattr(self, 'tooltip') and self.tooltip:
                self.tooltip.destroy()
                self.tooltip = None

        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)

    def on_closing(self):
        """Handle application closing - stop streams and save settings"""
        self.running = False
        self.stop_streams()
        self.save_settings()
        self.root.destroy()

    def auto_select_monitor(self):
        # First try to use saved setting if available
        if hasattr(self, 'saved_monitor_device') and self.saved_monitor_device:
            for i, (idx, name) in enumerate(self.device_list):
                if name == self.saved_monitor_device:
                    self.monitor_device_combo.current(i)
                    print(f"Using saved monitor device: {name}")
                    return

        # Try to auto-select a monitoring device (usually speakers/headphones)
        for i, (idx, name) in enumerate(self.device_list):
            if "speakers" in name.lower() or "headphones" in name.lower():
                self.monitor_device_combo.current(i)
                return

        # If nothing found, select the first device
        if self.device_list:
            self.monitor_device_combo.current(0)

    def restart_monitor(self, event=None):
        """Restart just the monitor stream if needed"""
        # Save device selection when changed
        self.save_settings()

        if self.running and self.self_listen:
            # Stop any existing monitor stream
            if self.monitor_stream is not None:
                self.monitor_stream.stop()
                self.monitor_stream.close()
                self.monitor_stream = None

            # Set up a new monitor stream
            self.setup_self_listen()

    def auto_load_sounds(self):
        """Auto-load sounds from the 'sounds' directory if it exists"""
        sounds_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")

        if os.path.exists(sounds_dir) and os.path.isdir(sounds_dir):
            # Find all audio files
            sound_files = []
            for file in os.listdir(sounds_dir):
                if file.lower().endswith((".wav", ".mp3", ".ogg", ".flac")):
                    sound_files.append(os.path.join(sounds_dir, file))

            # Load the sound files if any were found
            if sound_files:
                self.status_label.config(text=f"Auto-loading {len(sound_files)} sound files...")
                self.root.update()  # Update the UI to show the loading message

                # Clear previous sounds
                self.sound_files = []

                # Load each sound file
                for filepath in sound_files:
                    try:
                        # Load audio file
                        data, samplerate = sf.read(filepath, dtype='float32')

                        # Convert stereo to mono if needed
                        if len(data.shape) > 1 and data.shape[1] > 1:
                            data = np.mean(data, axis=1)

                        # Get additional sound info
                        sound_info = {
                            'sample_rate': samplerate,
                            'channels': 1,  # We convert to mono
                            'duration': len(data) / samplerate
                        }

                        # Store the sound data
                        self.sound_files.append((filepath, data, sound_info))

                    except Exception as e:
                        print(f"Error loading {filepath}: {e}", file=sys.stderr)

                # Update status
                self.status_label.config(text=f"Auto-loaded {len(self.sound_files)} sound file(s)")

                # Select the first sound if available
                if self.sound_files:
                    self.selected_sound_index = 0

                # Initialize filtered_sound_files with all sounds
                self.filtered_sound_files = list(range(len(self.sound_files)))

                # Restore saved buttons if available
                self.restore_saved_buttons()

    def restore_saved_buttons(self):
        """Restore buttons from saved settings"""
        if not hasattr(self, 'saved_buttons') or not self.saved_buttons:
            return

        # Clear any existing buttons
        self.sound_buttons = []

        # Remove any existing button widgets in the grid
        for widget in self.button_frame.winfo_children():
            widget.destroy()

        for button_info in self.saved_buttons:
            label = button_info.get('label', 'Sound Button')
            style_name = button_info.get('style', 'Default')
            sound_filename = button_info.get('sound_filename', '')

            # Find the sound index by matching filename
            sound_index = -1
            for i, (path, _, _) in enumerate(self.sound_files):
                if os.path.basename(path) == sound_filename:
                    sound_index = i
                    break

            # Only create the button if we found the sound
            if sound_index >= 0:
                self.add_sound_button(label, sound_index, style_name)

        # Update the canvas scrollregion
        self.button_frame.update_idletasks()
        self.button_canvas.configure(scrollregion=self.button_canvas.bbox("all"))

    def filter_sounds(self, *args):
        """Filter sounds based on search text"""
        search_text = self.search_var.get().lower()

        # Reset filtered sounds list
        self.filtered_sound_files = []

        # Filter sounds based on search text
        for i, (filepath, _, _) in enumerate(self.sound_files):
            filename = os.path.basename(filepath)
            if not search_text or search_text in filename.lower():
                self.filtered_sound_files.append(i)  # Store the original index

        # If we have a sound library dialog open, update its listbox
        if hasattr(self, '_sound_library_dialog') and self._sound_library_dialog is not None:
            try:
                if self._sound_library_dialog.winfo_exists():
                    self._update_sound_library_list()
            except:
                pass

    def toggle_self_listen(self):
        """Toggle self-listen (monitor) feature"""
        self.self_listen = self.monitor_var.get()

        if self.self_listen:
            self.setup_self_listen()
        else:
            # Close monitoring stream if it exists
            if hasattr(self, 'monitor_stream') and self.monitor_stream is not None:
                self.monitor_stream.stop_stream()
                self.monitor_stream.close()
                self.monitor_stream = None

        # Save the setting
        self.save_settings()

    def toggle_monitor_mic(self):
        """Toggle whether to include microphone in self-listen"""
        self.monitor_mic = self.monitor_mic_var.get()

        # Update status message
        if self.self_listen:
            if self.monitor_mic:
                self.status_label.config(text="Self-listen: Hearing both microphone and soundboard")
            else:
                self.status_label.config(text="Self-listen: Hearing only soundboard sounds")

        # Save the setting
        self.save_settings()

        # Restart monitor stream to apply change
        self.restart_monitor()

    def restart_streams(self, event=None):
        """Restart audio streams to apply new device selections or settings"""
        # Save device selection when changed
        self.save_settings()

        # Stop existing streams
        self.stop_streams()

        # Start new streams with updated settings
        self.start_streams()

        # Update status
        if hasattr(self, 'status_label'):
            self.status_label.config(text="Restarting audio streams...")

# Main execution
if __name__ == "__main__":
    try:
        # Create and run the application
        root = tk.Tk()
        app = SoundboardApp(root)
        root.protocol("WM_DELETE_WINDOW", app.on_closing)  # Ensure streams are closed on exit
        root.mainloop()
    except Exception as e:
        print(f"Error starting application: {e}", file=sys.stderr)
        # Show error in message box if possible
        try:
            import tkinter.messagebox as mb
            mb.showerror("Error", f"Error starting application: {e}")
        except:
            pass