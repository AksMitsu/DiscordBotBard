async def handle_playlist(queue_manager, guild_id, playlist_info, interaction=None):
    """Добавляет все треки плейлиста в очередь. Возвращает количество добавленных треков."""
    tracks = playlist_info['tracks']
    queue_manager.add_many(guild_id, tracks)
    return len(tracks)
