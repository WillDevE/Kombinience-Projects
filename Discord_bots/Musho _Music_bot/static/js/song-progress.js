// song-progress.js - Handles client-side song progress tracking

// Initialize timers for all currently playing songs
function initSongProgressTimers() {
    // Find all song elements
    const songElements = document.querySelectorAll('.current-song.with-image');
    
    if (songElements.length === 0) {
        console.debug('No song progress elements found on page load');
    } else {
        console.debug(`Initializing ${songElements.length} song progress elements`);
        songElements.forEach(songElement => {
            try {
                updateSingleSongProgress(songElement);
            } catch (e) {
                console.error('Error initializing song progress:', e);
            }
        });
    }
}

// Initialize progress tracking for a single song element
function initSingleSongProgress(songElement) {
    const startTime = parseInt(songElement.dataset.startTime || '0');
    let durationSeconds = parseInt(songElement.dataset.durationSeconds || '0');
    
    // If no duration seconds but there's a total-time element, try to parse it
    if (!durationSeconds) {
        const totalTimeEl = songElement.querySelector('.total-time');
        if (totalTimeEl) {
            durationSeconds = parseDuration(totalTimeEl.textContent);
            // Store the parsed duration back to the element for future use
            songElement.dataset.durationSeconds = durationSeconds;
        }
    }
    
    // Skip if we don't have valid timing data
    if (!startTime || !durationSeconds) {
        console.warn('Missing valid timing data for song progress', {startTime, durationSeconds});
        return;
    }
    
    // Calculate current progress
    const now = Math.floor(Date.now() / 1000);
    let elapsedSeconds = Math.max(0, now - startTime);
    
    // Cap at song duration
    if (elapsedSeconds > durationSeconds) {
        elapsedSeconds = durationSeconds;
    }
    
    // Store current state on the element
    songElement.dataset.elapsedSeconds = elapsedSeconds;
    
    // Update the UI
    updateSongProgressUI(songElement);
}

// Update all song progress elements
function updateAllSongProgress() {
    const songElements = document.querySelectorAll('.current-song.with-image');
    if (songElements.length === 0) {
        return; // Nothing to update
    }
    
    songElements.forEach(songElement => {
        try {
            updateSingleSongProgress(songElement);
        } catch (e) {
            console.error('Error updating song progress:', e);
        }
    });
}

// Update progress for a single song element
function updateSingleSongProgress(songElement) {
    // Check if this is a valid song element
    if (!songElement || !songElement.classList.contains('with-image')) {
        return;
    }
    
    // Get the duration in seconds - first try data attribute, then element content
    let durationSeconds = parseInt(songElement.dataset.durationSeconds || '0');
    if (!durationSeconds) {
        // Try to extract duration from the display
        const totalTimeEl = songElement.querySelector('.total-time');
        if (totalTimeEl) {
            const parsedDuration = parseDuration(totalTimeEl.textContent);
            songElement.dataset.durationSeconds = parsedDuration;
            durationSeconds = parsedDuration;
        }
        
        if (!durationSeconds) {
            return; // Skip if no duration information
        }
    }
    
    // Always calculate progress based on start time
    const startTime = parseInt(songElement.dataset.startTime || '0');
    if (!startTime) {
        return; // Skip if no start time information
    }
    
    // Calculate elapsed time (always fresh from the current timestamp)
    const now = Math.floor(Date.now() / 1000);
    let elapsedSeconds = Math.max(0, now - startTime);
    
    // Cap at song duration
    if (elapsedSeconds > durationSeconds) {
        elapsedSeconds = durationSeconds;
    }
    
    // Store updated state
    songElement.dataset.elapsedSeconds = elapsedSeconds;
    
    // Update the UI
    updateSongProgressUI(songElement);
}

// Update the UI elements for song progress
function updateSongProgressUI(songElement) {
    const durationSeconds = parseInt(songElement.dataset.durationSeconds || '0');
    const elapsedSeconds = parseInt(songElement.dataset.elapsedSeconds || '0');
    
    if (!durationSeconds) {
        return;
    }
    
    // Update time display
    const currentTimeElement = songElement.querySelector('.current-time');
    if (currentTimeElement) {
        currentTimeElement.textContent = formatTime(elapsedSeconds);
    }
    
    // Update progress bar
    const progressBar = songElement.querySelector('.progress-filled');
    if (progressBar) {
        const progressPercent = (elapsedSeconds / durationSeconds) * 100;
        progressBar.style.width = `${progressPercent}%`;
    }
}

// Format seconds to mm:ss format
function formatTime(seconds) {
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;
    return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`;
}

// Parse duration string like "3:45" to seconds
function parseDuration(durationStr) {
    if (!durationStr) return 0;
    
    const parts = durationStr.split(':');
    if (parts.length === 2) {
        return parseInt(parts[0]) * 60 + parseInt(parts[1]);
    } else if (parts.length === 3) {
        return parseInt(parts[0]) * 3600 + parseInt(parts[1]) * 60 + parseInt(parts[2]);
    }
    return 0;
}

function updateSongProgress() {
    // Get current timestamp
    const now = Math.floor(Date.now() / 1000);
    
    // Check if start_timestamp exists in localStorage
    const startTimestamp = parseInt(localStorage.getItem('song_start_timestamp') || '0');
    const songDuration = parseInt(localStorage.getItem('song_duration') || '0');
    
    if (startTimestamp > 0 && songDuration > 0) {
        // Calculate elapsed time
        const elapsed = now - startTimestamp;
        
        // Update DOM elements
        const progressBar = document.querySelector('.progress-filled');
        const elapsedTimeElement = document.getElementById('elapsed-time');
        const remainingTimeElement = document.getElementById('remaining-time');
        
        if (progressBar && elapsedTimeElement && remainingTimeElement) {
            // Animate the elements being updated
            elapsedTimeElement.classList.add('updated');
            remainingTimeElement.classList.add('updated');
            
            setTimeout(() => {
                elapsedTimeElement.classList.remove('updated');
                remainingTimeElement.classList.remove('updated');
            }, 1000);
            
            // Calculate progress percentage
            let progressPercentage = Math.min((elapsed / songDuration) * 100, 100);
            if (isNaN(progressPercentage) || !isFinite(progressPercentage)) {
                progressPercentage = 0;
            }

            // Update progress bar width
            progressBar.style.width = `${progressPercentage}%`;
            
            // Format times for display
            const elapsedFormatted = formatTime(elapsed);
            const remainingFormatted = formatTime(Math.max(songDuration - elapsed, 0));
            
            // Update time displays
            elapsedTimeElement.textContent = elapsedFormatted;
            remainingTimeElement.textContent = remainingFormatted;
        }
    }
}

// Add animation to stat values when they update
function animateStats() {
    const stats = document.querySelectorAll('.stat-value');
    stats.forEach(stat => {
        // Skip animation for uptime stat
        if (stat.id === 'uptime') return;
        
        stat.classList.add('updated');
        setTimeout(() => {
            stat.classList.remove('updated');
        }, 1000);
    });
}

// Call this function when stats get updated from the socket
socket.on('stats_update', function(data) {
    // Update stats with data
    if (data.total_songs) document.getElementById('total-songs-stat').textContent = data.total_songs;
    if (data.total_guilds) document.getElementById('total-guilds-stat').textContent = data.total_guilds;
    if (data.total_users) document.getElementById('total-users-stat').textContent = data.total_users;
    if (data.uptime) document.getElementById('uptime-stat').textContent = data.uptime;
    
    // Animate the updated stats
    animateStats();
});
