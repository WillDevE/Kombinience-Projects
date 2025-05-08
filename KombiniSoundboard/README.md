# Kombini Soundboard

A modern, customizable soundboard application with advanced audio features.

## Features

- **Customizable Soundboard**: Create buttons for your favorite sounds
- **Sound Library**: Organize and search your sound collection
- **Self-Listen**: Monitor your microphone input through the speakers
- **Advanced Playback Controls**: Seek, volume control, and visualizations
- **Multi-Device Support**: Select different audio devices for input and output

## Installation

1. Clone this repository:
   ```
   git clone https://github.com/yourusername/KombiniSoundboard.git
   cd KombiniSoundboard
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

## Usage

1. Run the application:
   ```
   python app.py
   ```

2. Load sound files:
   - Click the "Load Sounds" button
   - Select a directory containing sound files (WAV, MP3, OGG, FLAC)

3. Create sound buttons:
   - From the soundboard tab, click "Create Button"
   - Select a sound and customize the button appearance
   - Alternatively, browse the Sound Library tab and use "Add to Soundboard"

4. Use the self-listen feature:
   - Go to the Settings tab
   - Select your input device
   - Enable the "Self-Listen" checkbox
   - Adjust the volume as needed

## Audio Device Configuration

- In the Settings tab, you can select different input and output devices
- Click "Refresh Devices" to update the list of available devices
- Volume controls are available for both the soundboard and self-listen features

## Troubleshooting

If you encounter audio device issues:
1. Ensure your audio devices are properly connected
2. Check that the correct devices are selected in the Settings tab
3. Restart the application after connecting new devices

## Requirements

- Python 3.6 or higher
- Dependencies listed in requirements.txt

## License

This project is licensed under the MIT License - see the LICENSE file for details. 