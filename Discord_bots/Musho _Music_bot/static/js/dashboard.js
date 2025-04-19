// Dashboard.js - Handles dynamic updates and interactions

document.addEventListener('DOMContentLoaded', function() {
    // Perform initial data calculations
    calculateClientSideData();
    
    // Setup refresh functionality
    enableLiveRefresh();
    
    // Setup any refresh buttons
    const refreshButtons = document.querySelectorAll('.refresh-btn');
    refreshButtons.forEach(button => {
        button.addEventListener('click', function() {
            fetchLatestData();
        });
    });
    
    // Initialize song progress timers
    initSongProgressTimers();
    
    // Update song progress and uptime every second
    setInterval(updateDynamicElements, 1000);
});

// Store the last updated timestamp from the server
let lastUpdatedTimestamp = 0;

// Store bot start timestamp
let botStartTimestamp = 0;

// Calculate client-side data on load
function calculateClientSideData() {
    // Extract bot start timestamp if available
    const botStatsElement = document.getElementById('bot-stats-data');
    if (botStatsElement) {
        botStartTimestamp = parseInt(botStatsElement.dataset.startTimestamp || '0');
        lastUpdatedTimestamp = parseInt(botStatsElement.dataset.lastUpdated || '0');
    }
    
    // Calculate and update uptime immediately
    updateUptimeDisplay();
}

// Update uptime display
function updateUptimeDisplay() {
    if (botStartTimestamp > 0) {
        const now = Math.floor(Date.now() / 1000);
        const uptimeSeconds = now - botStartTimestamp;
        
        // Format uptime as HH:MM:SS
        const hours = Math.floor(uptimeSeconds / 3600);
        const minutes = Math.floor((uptimeSeconds % 3600) / 60);
        const seconds = uptimeSeconds % 60;
        
        const formattedUptime = `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
        
        // Update the uptime display
        const uptimeElement = document.getElementById('uptime');
        if (uptimeElement) {
            uptimeElement.textContent = formattedUptime;
        }
    }
}

// Update all dynamic elements that change with time
function updateDynamicElements() {
    // Update uptime
    updateUptimeDisplay();
    
    // Update song progress
    updateAllSongProgress();
}

// Live update functionality
function enableLiveRefresh() {
    // Set a longer interval for data fetching (10 seconds is more reasonable)
    setInterval(fetchLatestData, 10000);
    
    // Hide the refresh status container as we're using silent refreshes
    const refreshStatusEl = document.querySelector('.refresh-status');
    if (refreshStatusEl) {
        refreshStatusEl.style.display = 'none';
    }
}

// Format a timestamp for display
function formatTimestamp(timestamp) {
    const date = new Date(timestamp * 1000); // Convert to milliseconds
    const hours = date.getHours().toString().padStart(2, '0');
    const minutes = date.getMinutes().toString().padStart(2, '0');
    const seconds = date.getSeconds().toString().padStart(2, '0');
    
    return `${hours}:${minutes}:${seconds}`;
}

// Fetch latest data via API
function fetchLatestData() {
    // Include the last updated timestamp to avoid unnecessary data transfer
    fetch(`/api/stats?last_updated=${lastUpdatedTimestamp}`)
        .then(response => response.json())
        .then(data => {
            // Check if there are any changes
            if (data.no_changes) {
                console.debug('No changes detected from server');
                return;
            }
            
            // Update our last updated timestamp
            if (data.last_updated) {
                lastUpdatedTimestamp = data.last_updated;
            }
            
            // Only update changed data
            updateDashboard(data);
        })
        .catch(error => {
            console.error('Error fetching latest data:', error);
        });
}

// Update dashboard with new data
function updateDashboard(data) {
    // Update bot start timestamp if provided
    if (data.bot_stats && data.bot_stats.start_timestamp) {
        botStartTimestamp = data.bot_stats.start_timestamp;
    }
    
    // Update bot stats if provided
    if (data.bot_stats) {
        // We don't update uptime here anymore, it's calculated client-side
        updateElement('guild-count', data.bot_stats.guilds);
        updateElement('songs-played', data.bot_stats.total_songs_played);
        
        const voiceConnections = document.getElementById('voice-connections');
        if (voiceConnections) {
            updateElement('voice-connections', data.bot_stats.active_voice_channels);
        }
    }
    
    // Update song history if available
    if (data.song_history) {
        updateSongHistory(data.song_history);
    }
    
    // Update top songs if available
    if (data.top_songs) {
        updateTopSongs(data.top_songs);
    }
    
    // Update guild cards including now playing status
    if (data.guild_stats) {
        updateGuildCards(data.guild_stats);
    }
    
    // Debug info to console to track updates
    console.debug('Dashboard updated: ' + new Date().toLocaleTimeString());
}

// Helper function to update element content
function updateElement(id, value) {
    const element = document.getElementById(id);
    if (element && element.textContent !== String(value)) {
        element.textContent = value;
        // Add a subtle highlight effect for changed values, except for uptime
        if (id !== 'uptime') {
            element.classList.add('updated');
            setTimeout(() => {
                element.classList.remove('updated');
            }, 1000);
        }
    }
}

// Update song history section
function updateSongHistory(songs) {
    if (!songs || !songs.length) return;
    
    // Be more specific with our selector - only target the history section with ID "history"
    const songList = document.querySelector('#history .song-list');
    if (!songList) return;
    
    // Only update if there are new songs
    const firstSongTimestamp = songList.querySelector('.song-item .song-meta');
    if (firstSongTimestamp && songs[0] && firstSongTimestamp.textContent.includes(songs[0].timestamp)) {
        return; // No new songs
    }
    
    // Clear and rebuild song list
    songList.innerHTML = '';
    
    // Add up to 8 songs
    songs.slice(0, 8).forEach(song => {
        const songItem = document.createElement('div');
        songItem.className = 'song-item';
        
        // Create thumbnail
        if (song.thumbnail) {
            const img = document.createElement('img');
            img.src = song.thumbnail;
            img.alt = song.title;
            img.className = 'song-thumbnail';
            songItem.appendChild(img);
        } else {
            const thumbDiv = document.createElement('div');
            thumbDiv.className = 'song-thumbnail';
            thumbDiv.style.backgroundColor = '#7289da';
            thumbDiv.style.display = 'flex';
            thumbDiv.style.alignItems = 'center';
            thumbDiv.style.justifyContent = 'center';
            
            const icon = document.createElement('i');
            icon.className = 'fas fa-music';
            icon.style.color = 'white';
            icon.style.fontSize = '24px';
            thumbDiv.appendChild(icon);
            
            songItem.appendChild(thumbDiv);
        }
        
        // Create song info
        const songInfo = document.createElement('div');
        songInfo.className = 'song-info';
        
        // Create song title
        const songTitle = document.createElement('div');
        songTitle.className = 'song-title';
        
        const titleLink = document.createElement('a');
        titleLink.href = song.url;
        titleLink.target = '_blank';
        titleLink.title = song.title;
        titleLink.textContent = song.title;
        songTitle.appendChild(titleLink);
        
        // Create song metadata
        const songMeta = document.createElement('div');
        songMeta.className = 'song-meta';
        
        const timeIcon = document.createElement('i');
        timeIcon.className = 'fas fa-clock';
        songMeta.appendChild(timeIcon);
        songMeta.appendChild(document.createTextNode(' ' + song.timestamp));
        
        songMeta.appendChild(document.createElement('br'));
        
        const serverIcon = document.createElement('i');
        serverIcon.className = 'fas fa-server';
        songMeta.appendChild(serverIcon);
        songMeta.appendChild(document.createTextNode(' ' + song.guild));
        
        // Assemble components
        songInfo.appendChild(songTitle);
        songInfo.appendChild(songMeta);
        songItem.appendChild(songInfo);
        
        songList.appendChild(songItem);
    });
}

// Update top songs section
function updateTopSongs(songs) {
    if (!songs || !songs.length) return;
    
    const topList = document.querySelector('#top-songs .song-list');
    if (!topList) return;
    
    // Store current top song data for comparison
    const currentTopSongs = [];
    const currentItems = topList.querySelectorAll('.song-item');
    
    // Extract current song info for comparison
    currentItems.forEach(item => {
        const title = item.querySelector('.song-title a')?.textContent;
        const playCountText = item.querySelector('.song-meta')?.textContent || '';
        const playCountMatch = playCountText.match(/(\d+) plays/);
        const playCount = playCountMatch ? parseInt(playCountMatch[1]) : 0;
        
        if (title) {
            currentTopSongs.push({
                title: title,
                play_count: playCount
            });
        }
    });
    
    // Only update if there's a real change in the top songs list
    let needsUpdate = false;
    
    // If the number of current displayed songs is different, update
    if (currentItems.length !== songs.length) {
        needsUpdate = true;
    } else {
        // Check if songs or their order or play counts have changed
        for (let i = 0; i < songs.length; i++) {
            if (!currentTopSongs[i] || 
                currentTopSongs[i].title !== songs[i].title || 
                currentTopSongs[i].play_count !== songs[i].play_count) {
                needsUpdate = true;
                break;
            }
        }
    }
    
    if (!needsUpdate) {
        return; // No changes to top songs
    }
    
    // Clear and rebuild top songs list
    topList.innerHTML = '';
    
    // Add songs (up to 8)
    songs.slice(0, 8).forEach(song => {
        const songItem = document.createElement('div');
        songItem.className = 'song-item';
        
        // Create thumbnail
        if (song.thumbnail) {
            const img = document.createElement('img');
            img.src = song.thumbnail;
            img.alt = song.title;
            img.className = 'song-thumbnail';
            songItem.appendChild(img);
        } else {
            const thumbDiv = document.createElement('div');
            thumbDiv.className = 'song-thumbnail';
            thumbDiv.style.backgroundColor = '#7289da';
            thumbDiv.style.display = 'flex';
            thumbDiv.style.alignItems = 'center';
            thumbDiv.style.justifyContent = 'center';
            
            const icon = document.createElement('i');
            icon.className = 'fas fa-music';
            icon.style.color = 'white';
            icon.style.fontSize = '24px';
            thumbDiv.appendChild(icon);
            
            songItem.appendChild(thumbDiv);
        }
        
        // Create song info
        const songInfo = document.createElement('div');
        songInfo.className = 'song-info';
        
        // Create song title
        const songTitle = document.createElement('div');
        songTitle.className = 'song-title';
        
        const titleLink = document.createElement('a');
        titleLink.href = song.url;
        titleLink.target = '_blank';
        titleLink.title = song.title;
        titleLink.textContent = song.title;
        songTitle.appendChild(titleLink);
        
        // Create song metadata
        const songMeta = document.createElement('div');
        songMeta.className = 'song-meta';
        
        const playIcon = document.createElement('i');
        playIcon.className = 'fas fa-play-circle';
        songMeta.appendChild(playIcon);
        songMeta.appendChild(document.createTextNode(' ' + song.play_count + ' plays'));
        
        // Assemble components
        songInfo.appendChild(songTitle);
        songInfo.appendChild(songMeta);
        songItem.appendChild(songInfo);
        
        topList.appendChild(songItem);
    });
}

// Update guild cards if needed
function updateGuildCards(guildStats) {
    if (!guildStats) return;
    
    const guildList = document.querySelector('.guild-list');
    if (!guildList) return;
    
    // Keep track of processed guild IDs
    const processedGuildIds = new Set();
    
    // Update each guild card with new data
    Object.entries(guildStats).forEach(([guildId, guild]) => {
        processedGuildIds.add(guildId);
        
        // Find the guild card for this guild or create one if it doesn't exist
        let guildCard = guildList.querySelector(`.guild-card[data-guild-id="${guildId}"]`);
        
        // If the guild card doesn't exist, create a new one
        if (!guildCard) {
            guildCard = document.createElement('div');
            guildCard.className = 'guild-card';
            guildCard.dataset.guildId = guildId;
            
            // Create guild name
            const guildName = document.createElement('div');
            guildName.className = 'guild-name';
            guildName.textContent = guild.name;
            guildCard.appendChild(guildName);
            
            // Create initial song container (no-song state)
            const noSongContainer = document.createElement('div');
            noSongContainer.className = 'current-song no-song';
            noSongContainer.innerHTML = `
                <div class="current-song-status">
                    <div class="current-song-label">
                        <i class="fas fa-pause-circle"></i> Not Playing
                    </div>
                </div>
                <div class="current-song-details">
                    <div class="song-title">
                        <a href="#">Ready to play music!</a>
                    </div>
                    <div class="song-meta">
                        <i class="fas fa-info-circle"></i> Use /play to start a song
                    </div>
                </div>
            `;
            guildCard.appendChild(noSongContainer);
            
            // Create guild stats container
            const guildStats = document.createElement('div');
            guildStats.className = 'guild-stats';
            guildStats.innerHTML = `
                <div class="guild-stat">
                    <div class="guild-stat-value">${guild.member_count}</div>
                    <div class="guild-stat-label">Members</div>
                </div>
                <div class="guild-stat">
                    <div class="guild-stat-value">${guild.songs_played}</div>
                    <div class="guild-stat-label">Songs Played</div>
                </div>
                <div class="guild-stat">
                    <div class="guild-stat-value">${guild.queue_length}</div>
                    <div class="guild-stat-label">Queue Length</div>
                </div>
            `;
            guildCard.appendChild(guildStats);
            
            // Add the new card to the list
            guildList.appendChild(guildCard);
        }
        
        // If we found a card, update its values
        // Update member count
        const memberCount = guildCard.querySelector('.guild-stat:nth-child(1) .guild-stat-value');
        if (memberCount) memberCount.textContent = guild.member_count;
        
        // Update songs played
        const songsPlayed = guildCard.querySelector('.guild-stat:nth-child(2) .guild-stat-value');
        if (songsPlayed) songsPlayed.textContent = guild.songs_played;
        
        // Update queue length
        const queueLength = guildCard.querySelector('.guild-stat:nth-child(3) .guild-stat-value');
        if (queueLength) queueLength.textContent = guild.queue_length;
        
        // First, find the current song container
        let currentSongContainer = guildCard.querySelector('.current-song');
        
        // Handle the update of current song information
        if (guild.current_song) {
            // Server is playing a song
            
            // If the container doesn't have the right class, completely rebuild it
            const needsRebuild = !currentSongContainer || 
                (guild.current_song && currentSongContainer.classList.contains('no-song'));
            
            if (needsRebuild) {
                // Remove old container if it exists
                if (currentSongContainer) {
                    currentSongContainer.remove();
                }
                
                // Create new container with the right class
                currentSongContainer = document.createElement('div');
                currentSongContainer.className = 'current-song with-image';
                currentSongContainer.style.setProperty('--song-bg-image', `url('${guild.current_song.thumbnail}')`);
                
                // Set the start time data attribute for progress tracking
                if (guild.current_song.start_time_unix) {
                    currentSongContainer.dataset.startTime = guild.current_song.start_time_unix;
                }
                
                // Parse duration into seconds
                let durationSeconds = 0;
                if (guild.current_song.duration) {
                    const durationParts = guild.current_song.duration.split(':');
                    if (durationParts.length === 2) {
                        durationSeconds = (parseInt(durationParts[0]) * 60) + parseInt(durationParts[1]);
                    } else if (durationParts.length === 3) {
                        durationSeconds = (parseInt(durationParts[0]) * 3600) + (parseInt(durationParts[1]) * 60) + parseInt(durationParts[2]);
                    }
                }
                if (durationSeconds > 0) {
                    currentSongContainer.dataset.durationSeconds = durationSeconds;
                }
                
                // Create the inner HTML structure for playing status
                const songHTML = `
                    <div class="current-song-status">
                        <div class="current-song-label">
                            <i class="fas fa-play-circle"></i> Now Playing
                        </div>
                        <div class="song-time-display">
                            <span class="current-time">${guild.current_song.progress || '0:00'}</span> / <span class="total-time">${guild.current_song.duration}</span>
                        </div>
                    </div>
                    <div class="current-song-details">
                        <div class="song-title">
                            <a href="${guild.current_song.url}" target="_blank" title="${guild.current_song.title}">${guild.current_song.title}</a>
                        </div>
                        <div class="song-progress-bar">
                            <div class="progress-bar">
                                <div class="progress-filled" style="width: 0%"></div>
                            </div>
                        </div>
                    </div>
                `;
                
                // Add the song HTML
                currentSongContainer.innerHTML = songHTML;
                
                // Add the new container before the guild stats
                const guildStats = guildCard.querySelector('.guild-stats');
                if (guildStats) {
                    guildCard.insertBefore(currentSongContainer, guildStats);
                } else {
                    guildCard.appendChild(currentSongContainer);
                }
                
                // Initialize the progress bar
                updateSingleSongProgress(currentSongContainer);
            } else if (currentSongContainer.classList.contains('with-image')) {
                // Check if the song has changed
                const titleEl = currentSongContainer.querySelector('.song-title a');
                if (titleEl && titleEl.textContent !== guild.current_song.title) {
                    // Song has changed, update the container
                    currentSongContainer.style.setProperty('--song-bg-image', `url('${guild.current_song.thumbnail}')`);
                    
                    // Update song title and link
                    titleEl.href = guild.current_song.url;
                    titleEl.textContent = guild.current_song.title;
                    titleEl.title = guild.current_song.title;
                    
                    // Update duration display
                    const totalTime = currentSongContainer.querySelector('.total-time');
                    if (totalTime) {
                        totalTime.textContent = guild.current_song.duration;
                    }
                    
                    // Parse new duration
                    let durationSeconds = 0;
                    if (guild.current_song.duration) {
                        const durationParts = guild.current_song.duration.split(':');
                        if (durationParts.length === 2) {
                            durationSeconds = (parseInt(durationParts[0]) * 60) + parseInt(durationParts[1]);
                        } else if (durationParts.length === 3) {
                            durationSeconds = (parseInt(durationParts[0]) * 3600) + (parseInt(durationParts[1]) * 60) + parseInt(durationParts[2]);
                        }
                    }
                    if (durationSeconds > 0) {
                        currentSongContainer.dataset.durationSeconds = durationSeconds;
                    }
                    
                    // Set the start time data attribute for progress tracking
                    if (guild.current_song.start_time_unix) {
                        currentSongContainer.dataset.startTime = guild.current_song.start_time_unix;
                    }
                }
                
                // Always update progress time (server might provide better data)
                const currentTime = currentSongContainer.querySelector('.current-time');
                if (currentTime && guild.current_song.progress) {
                    currentTime.textContent = guild.current_song.progress;
                }
                
                // Make sure progress tracking is working
                updateSingleSongProgress(currentSongContainer);
            }
        } else if (currentSongContainer && currentSongContainer.classList.contains('with-image')) {
            // Server is not playing a song, but we show a song - replace with "no-song" container
            // Remove the existing container
            currentSongContainer.remove();
            
            // Create a new "no-song" container
            const noSongContainer = document.createElement('div');
            noSongContainer.className = 'current-song no-song';
            noSongContainer.innerHTML = `
                <div class="current-song-status">
                    <div class="current-song-label">
                        <i class="fas fa-pause-circle"></i> Not Playing
                    </div>
                </div>
                <div class="current-song-details">
                    <div class="song-title">
                        <a href="#">Ready to play music!</a>
                    </div>
                    <div class="song-meta">
                        <i class="fas fa-info-circle"></i> Use /play to start a song
                    </div>
                </div>
            `;
            
            // Add the new container before the guild stats
            const guildStats = guildCard.querySelector('.guild-stats');
            if (guildStats) {
                guildCard.insertBefore(noSongContainer, guildStats);
            } else {
                guildCard.appendChild(noSongContainer);
            }
        }
    });
    
    // Remove guild cards for servers that are no longer connected
    const existingCards = guildList.querySelectorAll('.guild-card');
    existingCards.forEach(card => {
        const cardGuildId = card.dataset.guildId;
        if (!processedGuildIds.has(cardGuildId)) {
            card.remove();
        }
    });
}
