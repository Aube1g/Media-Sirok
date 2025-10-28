import os
import requests
import logging
import tempfile
import asyncio
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import yt_dlp
import re

# ТВОИ РАБОЧИЕ API
YOUTUBE_API_KEY = "AIzaSyDRb5v81fCgHXjGUdaYYi2JQVr9ZWhZzds"
AUDD_API_TOKEN = "68131322b91e192191630d5fcd32614e"
TELEGRAM_BOT_TOKEN = "8466849152:AAHmgdx4vZ-Q6PqxtGnIXLTXGZ-zAeWZLRs"

# АДМИН ПАРОЛЬ
ADMIN_PASSWORD = "admin123"
ADMIN_USERS = []

# Настройка логирования БЕЗ HTTP запросов
class NoHTTPFilter(logging.Filter):
    def filter(self, record):
        return not record.getMessage().startswith('HTTP Request:')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Убираем HTTP логи из консоли
for handler in logging.getLogger().handlers:
    if isinstance(handler, logging.StreamHandler):
        handler.addFilter(NoHTTPFilter())

class Database:
    def __init__(self):
        self.conn = sqlite3.connect('music_bot.db', check_same_thread=False)
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_banned BOOLEAN DEFAULT FALSE,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                query TEXT,
                results_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                track_title TEXT,
                artist TEXT,
                source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        self.conn.commit()
    
    def add_user(self, user_id, username, first_name, last_name):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name))
        self.conn.commit()
    
    def add_search_history(self, user_id, query, results_count):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO search_history (user_id, query, results_count)
            VALUES (?, ?, ?)
        ''', (user_id, query, results_count))
        self.conn.commit()
    
    def add_download_history(self, user_id, track_title, artist, source):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO download_history (user_id, track_title, artist, source)
            VALUES (?, ?, ?, ?)
        ''', (user_id, track_title, artist, source))
        self.conn.commit()
    
    def get_user_stats(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM search_history WHERE user_id = ?', (user_id,))
        searches = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM download_history WHERE user_id = ?', (user_id,))
        downloads = cursor.fetchone()[0]
        return {'searches': searches, 'downloads': downloads}
    
    def get_all_users(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT user_id, username, first_name, last_name, is_banned, is_admin, 
                   (SELECT COUNT(*) FROM search_history WHERE user_id = users.user_id) as search_count,
                   (SELECT COUNT(*) FROM download_history WHERE user_id = users.user_id) as download_count
            FROM users 
            ORDER BY created_at DESC
        ''')
        return cursor.fetchall()
    
    def ban_user(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET is_banned = TRUE WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def unban_user(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET is_banned = FALSE WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def make_admin(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET is_admin = TRUE WHERE user_id = ?', (user_id,))
        self.conn.commit()
        if user_id not in ADMIN_USERS:
            ADMIN_USERS.append(user_id)
    
    def is_user_banned(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result and result[0]
    
    def is_user_admin(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT is_admin FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result and result[0]

db = Database()

class MusicBot:
    def __init__(self):
        self.youtube_key = YOUTUBE_API_KEY
        self.audd_token = AUDD_API_TOKEN
        
    async def recognize_audio(self, audio_file_path: str) -> dict:
        """Распознавание аудио через AudD с улучшенной обработкой ошибок"""
        try:
            url = "https://api.audd.io/"
            with open(audio_file_path, 'rb') as audio_file:
                files = {'file': audio_file}
                data = {
                    'api_token': self.audd_token,
                    'return': 'spotify,youtube,deezer',
                    'method': 'recognize'
                }
                response = requests.post(url, files=files, data=data, timeout=30)
                
            if response.status_code == 200:
                result = response.json()
                if result['status'] == 'success' and result['result']:
                    # Проверяем наличие обязательных полей
                    if 'title' in result['result'] and 'artist' in result['result']:
                        return result['result']
                    else:
                        logger.warning("AudD вернул результат без обязательных полей")
        except Exception as e:
            logger.error(f"AudD error: {e}")
        return None
    
    async def search_music(self, query: str = None, audio_file_path: str = None) -> dict:
        """Улучшенный поиск музыки с обработкой ошибок"""
        results = {}
        
        if audio_file_path:
            recognized = await self.recognize_audio(audio_file_path)
            if recognized:
                results['recognized'] = recognized
                query = f"{recognized.get('title', '')} {recognized.get('artist', '')}".strip()
                logger.info(f"Распознано: {query}")
        
        if query:
            # Параллельный поиск по всем источникам
            try:
                youtube_results = await self.search_youtube_music(query)
                deezer_results = await self.search_deezer(query)
                soundcloud_results = await self.search_soundcloud(query)
                
                if youtube_results:
                    results['youtube'] = youtube_results
                if deezer_results:
                    results['deezer'] = deezer_results
                if soundcloud_results:
                    results['soundcloud'] = soundcloud_results
                    
            except Exception as e:
                logger.error(f"Search error: {e}")
                
        return results
    
    async def search_deezer(self, query: str) -> list:
        """Поиск музыки через Deezer API с улучшенной обработкой"""
        try:
            # Очищаем запрос от специальных символов
            clean_query = re.sub(r'[^\w\s]', '', query)
            url = f"https://api.deezer.com/search"
            params = {'q': clean_query, 'limit': 15}
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                tracks = []
                for item in data.get('data', []):
                    # Проверяем наличие всех необходимых полей
                    if all(key in item for key in ['id', 'title', 'artist', 'album']):
                        tracks.append({
                            'id': str(item['id']),
                            'title': item['title'],
                            'artist': item['artist']['name'],
                            'album': item['album']['title'],
                            'duration': item.get('duration', 0),
                            'preview': item.get('preview', ''),
                            'cover_small': item['album'].get('cover_small', ''),
                            'cover_medium': item['album'].get('cover_medium', ''),
                            'cover_big': item['album'].get('cover_big', ''),
                            'source': 'deezer'
                        })
                logger.info(f"Deezer найдено треков: {len(tracks)}")
                return tracks
        except Exception as e:
            logger.error(f"Deezer search error: {e}")
        return []
    
    async def search_soundcloud(self, query: str) -> list:
        """Поиск на SoundCloud через yt-dlp"""
        try:
            ydl_opts = {
                'quiet': True,
                'extract_flat': True,
                'default_search': 'scsearch10:'
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(query, download=False)
                    tracks = []
                    if info and 'entries' in info:
                        for entry in info['entries'][:8]:  # Ограничиваем количество
                            if entry and 'id' in entry:
                                tracks.append({
                                    'id': entry['id'],
                                    'title': entry.get('title', 'Unknown'),
                                    'url': entry.get('url', ''),
                                    'uploader': entry.get('uploader', 'Unknown'),
                                    'duration': entry.get('duration', 0),
                                    'source': 'soundcloud'
                                })
                        logger.info(f"SoundCloud найдено треков: {len(tracks)}")
                        return tracks
                except Exception as e:
                    logger.warning(f"SoundCloud search failed: {e}")
                    return []
        except Exception as e:
            logger.error(f"SoundCloud error: {e}")
        return []
    
    async def search_youtube_music(self, query: str) -> list:
        """Улучшенный поиск музыки на YouTube с обработкой ошибок"""
        try:
            # Сначала пробуем через YouTube API
            search_queries = [
                f"{query} official audio",
                f"{query} music",
                f"{query} song"
            ]
            
            all_videos = []
            for search_query in search_queries:
                try:
                    url = "https://www.googleapis.com/youtube/v3/search"
                    params = {
                        'part': 'snippet',
                        'q': search_query,
                        'type': 'video',
                        'maxResults': 5,
                        'key': self.youtube_key
                    }
                    
                    response = requests.get(url, params=params, timeout=10)
                    if response.status_code == 200:
                        data = response.json()
                        for item in data.get('items', []):
                            if 'id' in item and 'videoId' in item['id']:
                                video_info = {
                                    'id': item['id']['videoId'],
                                    'title': item['snippet']['title'],
                                    'channel': item['snippet'].get('channelTitle', 'Unknown'),
                                    'url': f"https://youtu.be/{item['id']['videoId']}",
                                    'thumbnail': item['snippet']['thumbnails']['default']['url'],
                                    'source': 'youtube'
                                }
                                if not any(v['id'] == video_info['id'] for v in all_videos):
                                    all_videos.append(video_info)
                except Exception as e:
                    logger.warning(f"YouTube API search failed for '{search_query}': {e}")
                    continue
            
            # Если YouTube API не дал результатов, используем yt-dlp
            if not all_videos:
                all_videos = await self.search_youtube_alternative(query)
            
            logger.info(f"YouTube найдено видео: {len(all_videos)}")
            return all_videos[:15]
                
        except Exception as e:
            logger.error(f"YouTube search error: {e}")
            return await self.search_youtube_alternative(query)
    
    async def search_youtube_alternative(self, query: str) -> list:
        """Альтернативный поиск через yt-dlp с улучшенной обработкой"""
        try:
            ydl_opts = {
                'quiet': True,
                'extract_flat': True,
                'default_search': 'ytsearch15'
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(query, download=False)
                    videos = []
                    if info and 'entries' in info:
                        for entry in info['entries'][:10]:
                            if entry and 'id' in entry:
                                videos.append({
                                    'id': entry['id'],
                                    'title': entry.get('title', 'Unknown'),
                                    'url': f"https://youtu.be/{entry['id']}",
                                    'channel': entry.get('uploader', 'Unknown'),
                                    'thumbnail': entry.get('thumbnail', ''),
                                    'source': 'youtube'
                                })
                    logger.info(f"Альтернативный поиск: {len(videos)} видео")
                    return videos
                except Exception as e:
                    logger.warning(f"YouTube alternative search failed: {e}")
                    return []
        except Exception as e:
            logger.error(f"YouTube alternative error: {e}")
            return []
    
    async def download_youtube_audio(self, video_url: str) -> dict:
        """Скачивает аудио с YouTube с улучшенной обработкой ошибок"""
        try:
            temp_dir = tempfile.gettempdir()
            
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(temp_dir, '%(title).100s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
            }
            
            # Проверяем наличие ffmpeg
            try:
                import subprocess
                result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                if result.returncode == 0:
                    ydl_opts.update({
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192',
                        }],
                    })
            except:
                pass
            
            logger.info(f"Скачиваем YouTube: {video_url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                file_path = ydl.prepare_filename(info)
                
                if 'postprocessors' in ydl_opts:
                    file_path = os.path.splitext(file_path)[0] + '.mp3'
                
                if os.path.exists(file_path):
                    logger.info(f"Файл готов: {file_path}")
                    return {
                        'file_path': file_path,
                        'title': info.get('title', 'Unknown'),
                        'duration': info.get('duration', 0)
                    }
                else:
                    logger.error("Файл не был создан после скачивания")
                    
        except Exception as e:
            logger.error(f"YouTube download error: {e}")
        return None

    async def download_soundcloud_track(self, track_url: str) -> dict:
        """Скачивает трек с SoundCloud"""
        try:
            temp_dir = tempfile.gettempdir()
            
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(temp_dir, '%(title).100s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
            }
            
            # Проверяем наличие ffmpeg
            try:
                import subprocess
                result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
                if result.returncode == 0:
                    ydl_opts.update({
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192',
                        }],
                    })
            except:
                pass
            
            logger.info(f"Скачиваем SoundCloud: {track_url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(track_url, download=True)
                file_path = ydl.prepare_filename(info)
                
                if 'postprocessors' in ydl_opts:
                    file_path = os.path.splitext(file_path)[0] + '.mp3'
                
                if os.path.exists(file_path):
                    logger.info(f"SoundCloud файл готов: {file_path}")
                    return {
                        'file_path': file_path,
                        'title': info.get('title', 'Unknown'),
                        'duration': info.get('duration', 0)
                    }
                else:
                    logger.error("SoundCloud файл не был создан после скачивания")
                    
        except Exception as e:
            logger.error(f"SoundCloud download error: {e}")
        return None

    async def download_deezer_preview(self, track_data: dict) -> dict:
        """Скачивает превью трека с Deezer с улучшенной обработкой"""
        try:
            if not track_data.get('preview'):
                logger.error("No preview URL available")
                return None
                
            # Скачиваем превью
            preview_url = track_data['preview']
            logger.info(f"Downloading Deezer preview")
            response = requests.get(preview_url, timeout=30)
            
            if response.status_code == 200:
                # Создаем временный файл
                temp_dir = tempfile.gettempdir()
                safe_title = re.sub(r'[^\w\s]', '', track_data['title'])[:50]
                filename = f"deezer_{track_data['id']}_{safe_title}.mp3"
                file_path = os.path.join(temp_dir, filename)
                
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                
                logger.info(f"Preview downloaded to: {file_path}")
                
                # Скачиваем обложку
                cover_url = track_data.get('cover_big') or track_data.get('cover_medium')
                cover_path = None
                if cover_url:
                    try:
                        cover_response = requests.get(cover_url, timeout=30)
                        if cover_response.status_code == 200:
                            cover_filename = f"cover_{track_data['id']}.jpg"
                            cover_path = os.path.join(temp_dir, cover_filename)
                            with open(cover_path, 'wb') as f:
                                f.write(cover_response.content)
                            logger.info(f"Cover downloaded")
                    except Exception as e:
                        logger.error(f"Error downloading cover: {e}")
                        cover_path = None
                
                return {
                    'file_path': file_path,
                    'title': track_data['title'],
                    'artist': track_data['artist'],
                    'album': track_data.get('album', ''),
                    'cover_path': cover_path,
                    'duration': 30,
                    'source': 'deezer'
                }
            else:
                logger.error(f"Failed to download preview: HTTP {response.status_code}")
                
        except Exception as e:
            logger.error(f"Deezer preview download error: {e}")
        return None

    def create_search_keyboard(self, results: dict, page: int = 0, page_size: int = 8):
        keyboard = []
        
        if 'recognized' in results:
            track = results['recognized']
            # Безопасное создание callback_data
            safe_title = track.get('title', '')[:15].replace(' ', '_')
            safe_artist = track.get('artist', '')[:10].replace(' ', '_')
            keyboard.append([
                InlineKeyboardButton(
                    f"🎵 РАСПОЗНАНО: {track.get('title', 'Unknown')[:20]}...", 
                    callback_data=f"rec_{safe_title}_{safe_artist}"
                )
            ])
            keyboard.append([])
        
        # Объединяем все результаты
        all_results = []
        source_icons = {'youtube': '📹', 'deezer': '🎵', 'soundcloud': '🎧'}
        
        for source in ['deezer', 'youtube', 'soundcloud']:
            if source in results and results[source]:
                icon = source_icons.get(source, '🎵')
                for item in results[source]:
                    if 'id' in item and 'title' in item:  # Проверяем обязательные поля
                        item['source'] = source
                        item['icon'] = icon
                        all_results.append(item)
        
        # Пагинация
        start_idx = page * page_size
        end_idx = start_idx + page_size
        current_results = all_results[start_idx:end_idx]
        
        for i, item in enumerate(current_results, start_idx + 1):
            button_text = f"{item['icon']} {i}. {item['title'][:25]}..."
            # Безопасное создание callback_data
            safe_id = str(item['id']).replace('_', '-')  # Заменяем _ на - чтобы не ломало разбор
            callback_data = f"track_{item['source']}_{safe_id}_{page}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        # Пагинация
        total_pages = max(1, (len(all_results) + page_size - 1) // page_size)
        pagination_buttons = []
        
        if page > 0:
            pagination_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{page-1}"))
        
        pagination_buttons.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="current_page"))
        
        if page < total_pages - 1:
            pagination_buttons.append(InlineKeyboardButton("Далее ➡️", callback_data=f"page_{page+1}"))
        
        if pagination_buttons:
            keyboard.append(pagination_buttons)
        
        # Основные кнопки
        keyboard.extend([
            [InlineKeyboardButton("🔄 Новый поиск", callback_data="new_search")],
            [InlineKeyboardButton("🎤 Распознать аудио", callback_data="recognize_audio")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ])
        
        return InlineKeyboardMarkup(keyboard)
    
    def create_track_keyboard(self, track_data: dict = None, source: str = None, item_id: str = None, page: int = 0):
        if track_data and source == 'deezer':
            safe_id = str(track_data['id']).replace('_', '-')
            safe_data = f"dl_deezer_{safe_id}_{page}"
        elif track_data and source == 'soundcloud':
            safe_id = str(item_id).replace('_', '-')
            safe_data = f"dl_soundcloud_{safe_id}_{page}"
        elif track_data:
            safe_title = track_data.get('title', '')[:15].replace(' ', '_')
            safe_artist = track_data.get('artist', '')[:10].replace(' ', '_')
            safe_data = f"dl_rec_{safe_title}_{safe_artist}"
        else:
            safe_id = str(item_id).replace('_', '-')
            safe_data = f"dl_youtube_{safe_id}_{page}"
        
        keyboard = [
            [InlineKeyboardButton("⬇️ Скачать MP3", callback_data=safe_data)],
            [InlineKeyboardButton("⬅️ Назад к результатам", callback_data=f"back_{page}")],
            [InlineKeyboardButton("🔍 Новый поиск", callback_data="new_search")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]
        
        return InlineKeyboardMarkup(keyboard)
    
    def create_track_info_message(self, track_data: dict, source: str) -> str:
        """Создает красивое описание трека с безопасным доступом к полям"""
        title = track_data.get('title', 'Неизвестно')
        
        if source == 'deezer':
            duration_sec = track_data.get('duration', 0)
            duration_formatted = f"{duration_sec // 60}:{duration_sec % 60:02d}"
            
            message = (
                f"> 🎵 *{title}*\n\n"
                f"*👤 Артист:* {track_data.get('artist', 'Неизвестно')}\n"
                f"*💿 Альбом:* {track_data.get('album', 'Неизвестно')}\n"
                f"*⏱ Длительность:* {duration_formatted}\n"
                f"*🎼 Источник:* ᴅᴇᴇᴢᴇʀ\n\n"
                f"_Будет скачано 30\\-секундное превью_"
            )
        elif source == 'soundcloud':
            message = (
                f"> 🎧 *{title}*\n\n"
                f"*👤 Автор:* {track_data.get('uploader', 'Неизвестно')}\n"
                f"*🎼 Источник:* sᴏᴜɴᴅᴄʟᴏᴜᴅ\n\n"
                f"_Будет скачан полный трек_"
            )
        else:  # YouTube
            message = (
                f"> 📹 *{title}*\n\n"
                f"*🎬 Канал:* {track_data.get('channel', 'Неизвестно')}\n"
                f"*🎼 Источник:* [ʏᴏᴜᴛᴜʙᴇ]({track_data.get('url', '')})\n\n"
                f"_Будет скачан полный трек_"
            )
        
        return message

    def create_admin_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton("👥 Управление пользователями", callback_data="admin_users")],
            [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def create_users_keyboard(self, users, page: int = 0, page_size: int = 10):
        keyboard = []
        start_idx = page * page_size
        end_idx = start_idx + page_size
        current_users = users[start_idx:end_idx]
        
        for user in current_users:
            user_id, username, first_name, last_name, is_banned, is_admin, search_count, download_count = user
            name = first_name or username or f"User {user_id}"
            status = "🔴" if is_banned else "🟢"
            admin = " 👑" if is_admin else ""
            
            keyboard.append([
                InlineKeyboardButton(f"{status} {name}{admin}", callback_data=f"user_detail_{user_id}")
            ])
        
        pagination_buttons = []
        if page > 0:
            pagination_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"users_page_{page-1}"))
        
        if end_idx < len(users):
            pagination_buttons.append(InlineKeyboardButton("Далее ➡️", callback_data=f"users_page_{page+1}"))
        
        if pagination_buttons:
            keyboard.append(pagination_buttons)
        
        keyboard.append([InlineKeyboardButton("⬅️ В админку", callback_data="admin_panel")])
        
        return InlineKeyboardMarkup(keyboard)
    
    def create_user_management_keyboard(self, user_id, is_banned):
        ban_text = "🔒 Забанить" if not is_banned else "🔓 Разбанить"
        ban_callback = f"ban_{user_id}" if not is_banned else f"unban_{user_id}"
        
        keyboard = [
            [InlineKeyboardButton(ban_text, callback_data=ban_callback)],
            [InlineKeyboardButton("👑 Сделать админом", callback_data=f"make_admin_{user_id}")],
            [InlineKeyboardButton("📊 Статистика пользователя", callback_data=f"user_stats_{user_id}")],
            [InlineKeyboardButton("⬅️ К списку пользователей", callback_data="admin_users")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def create_broadcast_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("📢 Всем пользователям", callback_data="broadcast_all")],
            [InlineKeyboardButton("👥 Только активным", callback_data="broadcast_active")],
            [InlineKeyboardButton("⬅️ В админку", callback_data="admin_panel")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

music_bot = MusicBot()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username, user.first_name, user.last_name)
    
    # ФИКС: Проверка бана должна быть сразу после добавления пользователя
    if db.is_user_banned(user.id):
        await update.message.reply_text("❌ Вы заблокированы в этом боте.")
        return
    
    keyboard = [
        [InlineKeyboardButton("🔍 Поиск по названию", callback_data="text_search")],
        [InlineKeyboardButton("🎤 Распознать аудио", callback_data="recognize_audio")],
        [InlineKeyboardButton("📊 Моя статистика", callback_data="my_stats")]
    ]
    
    if user.id in ADMIN_USERS or db.is_user_admin(user.id):
        if user.id not in ADMIN_USERS:
            ADMIN_USERS.append(user.id)
        keyboard.append([InlineKeyboardButton("👑 Админка", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "> 🎵 *Добро пожаловать в музыкального бота\\!*\n\n"
        "*Я могу:*\n"
        "• 🔍 Искать музыку в ᴅᴇᴇᴢᴇʀ, ʏᴏᴜᴛᴜʙᴇ и sᴏᴜɴᴅᴄʟᴏᴜᴅ\n" 
        "• 🎤 Распознавать треки из аудио\n"
        "• 📥 Скачивать в MP3\n\n"
        "> *Выбери действие:*"
    )
    
    # ФИКС: Проверяем, откуда пришел запрос
    if update.callback_query:
        await update.callback_query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='MarkdownV2')

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # ФИКС: Проверка бана перед обработкой аудио
    if db.is_user_banned(user.id):
        await update.message.reply_text("❌ Вы заблокированы в этом боте.")
        return
    
    try:
        if update.message.voice:
            audio_file = await update.message.voice.get_file()
        elif update.message.audio:
            audio_file = await update.message.audio.get_file()
        else:
            await update.message.reply_text("❌ Отправь голосовое сообщение или аудиофайл")
            return
            
        file_path = f"temp_audio_{update.update_id}.mp3"
        await audio_file.download_to_drive(file_path)
        
        processing_msg = await update.message.reply_text("> 🎤 Распознаю аудио\\.\\.\\.")
        
        results = await music_bot.search_music(audio_file_path=file_path)
        
        if os.path.exists(file_path):
            os.remove(file_path)
        
        if 'recognized' in results:
            track = results['recognized']
            context.user_data['last_track'] = track
            context.user_data['last_results'] = results
            context.user_data['last_query'] = f"{track.get('title', '')} {track.get('artist', '')}".strip()
            
            response_text = (
                "> ✅ *Трек распознан\\!*\n\n"
                f"*🎵 {track.get('title', 'Неизвестно')}*\n"
                f"*👤 Артист:* {track.get('artist', 'Неизвестно')}\n"
                f"*💿 Альбом:* {track.get('album', 'Неизвестно')}\n\n"
                "> _Что делаем с этим треком?_"
            )
            
            keyboard = music_bot.create_track_keyboard(track_data=track)
            await processing_msg.edit_text(response_text, reply_markup=keyboard, parse_mode='MarkdownV2')
        else:
            await processing_msg.edit_text("> ❌ Не удалось распознать трек\\. Попробуй другой фрагмент\\.")
            
    except Exception as e:
        logger.error(f"Audio error: {e}")
        await update.message.reply_text("> ❌ Ошибка при обработке аудио")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # ФИКС: Проверка бана перед обработкой текста
    if db.is_user_banned(user.id):
        await update.message.reply_text("> ❌ Вы заблокированы в этом боте.")
        return
    
    query = update.message.text.strip()
    
    if context.user_data.get('waiting_for_admin_password'):
        if query == ADMIN_PASSWORD:
            db.make_admin(user.id)
            ADMIN_USERS.append(user.id)
            await update.message.reply_text("> ✅ Вы успешно вошли в админку\\!")
            context.user_data['waiting_for_admin_password'] = False
            await start(update, context)
        else:
            await update.message.reply_text("> ❌ Неверный пароль\\!")
            context.user_data['waiting_for_admin_password'] = False
        return
    
    if context.user_data.get('waiting_for_broadcast'):
        await handle_broadcast_message(update, context, query)
        return
    
    if query.startswith('/admin'):
        await handle_admin_command(update, context)
        return
        
    if query.startswith('/'):
        return
        
    await perform_search(update, context, query)

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, page: int = 0):
    user = update.effective_user
    
    # Создаем клавиатуру с кнопкой отмены
    cancel_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отменить поиск", callback_data="main_menu")]
    ])
    
    if hasattr(update, 'message'):
        search_message = await update.message.reply_text(
            f"> 🔍 Ищу музыку: *{query}*\\.\\.\\.", 
            reply_markup=cancel_keyboard,
            parse_mode='MarkdownV2'
        )
    else:
        search_message = await update.callback_query.edit_message_text(
            f"> 🔍 Ищу музыку: *{query}*\\.\\.\\.", 
            reply_markup=cancel_keyboard,
            parse_mode='MarkdownV2'
        )
    
    results = await music_bot.search_music(query=query)
    
    total_results = sum(len(results.get(source, [])) for source in ['deezer', 'youtube', 'soundcloud'])
    db.add_search_history(user.id, query, total_results)
    
    context.user_data['last_results'] = results
    context.user_data['last_query'] = query
    context.user_data['current_page'] = page
    
    if total_results == 0:
        await search_message.edit_text(
            "> ❌ Ничего не найдено\\. Попробуй другой запрос\\.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Новый поиск", callback_data="text_search")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
            ]),
            parse_mode='MarkdownV2'
        )
        return
    
    reply_markup = music_bot.create_search_keyboard(results, page)
    
    sources_found = []
    if results.get('deezer'):
        sources_found.append(f"ᴅᴇᴇᴢᴇʀ \\({len(results['deezer'])}\\)")
    if results.get('youtube'):
        sources_found.append(f"ʏᴏᴜᴛᴜʙᴇ \\({len(results['youtube'])}\\)")
    if results.get('soundcloud'):
        sources_found.append(f"sᴏᴜɴᴅᴄʟᴏᴜᴅ \\({len(results['soundcloud'])}\\)")
    
    sources_text = ", ".join(sources_found)
    
    response_text = (
        f"> 🎵 *Результаты для:* `{query}`\n"
        f"*📊 Найдено треков:* {total_results}\n"
        f"*📄 Страница* {page + 1}\n\n"
        f"> *Выбери трек для скачивания:*"
    )
    
    await search_message.edit_text(response_text, reply_markup=reply_markup, parse_mode='MarkdownV2')

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    
    # ФИКС: Проверка бана перед обработкой callback
    if db.is_user_banned(user.id):
        await query.edit_message_text("> ❌ Вы заблокированы в этом боте\\.")
        return
    
    data = query.data
    
    if data == "main_menu":
        await show_main_menu(query, context)
        return
        
    elif data == "text_search":
        await query.edit_message_text(
            "> 🔍 *Введи название трека или артиста:*\n\n"
            "_Пример: Lana Del Radio Young_",
            parse_mode='MarkdownV2'
        )
        context.user_data['waiting_for_text_search'] = True
        return
        
    elif data == "recognize_audio":
        await query.edit_message_text(
            "> 🎤 *Запиши голосовое сообщение или отправь аудиофайл:*\n\n"
            "_Достаточно 10\\-15 секунд для распознавания_",
            parse_mode='MarkdownV2'
        )
        return
        
    elif data == "my_stats":
        stats = db.get_user_stats(user.id)
        await query.edit_message_text(
            f"> 📊 *Твоя статистика:*\n\n"
            f"*🔍 Поисков:* {stats['searches']}\n"
            f"*📥 Скачиваний:* {stats['downloads']}\n\n"
            f"> _Продолжаем музыку\\!_ 🎵",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Поиск музыки", callback_data="text_search")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
            ]),
            parse_mode='MarkdownV2'
        )
        return
        
    elif data == "admin_panel":
        if user.id in ADMIN_USERS or db.is_user_admin(user.id):
            if user.id not in ADMIN_USERS:
                ADMIN_USERS.append(user.id)
            await query.edit_message_text(
                "> 👑 *Панель администратора*\n\n"
                "*Выбери действие:*",
                reply_markup=music_bot.create_admin_keyboard(),
                parse_mode='MarkdownV2'
            )
        else:
            await query.edit_message_text(
                "> ❌ У тебя нет доступа к админке\\!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
                ]),
                parse_mode='MarkdownV2'
            )
        return
        
    elif data == "admin_stats":
        if user.id not in ADMIN_USERS and not db.is_user_admin(user.id):
            await query.answer("❌ Нет доступа")
            return
            
        all_users = db.get_all_users()
        total_users = len(all_users)
        active_users = len([u for u in all_users if not u[4]])  # is_banned
        banned_users = len([u for u in all_users if u[4]])
        total_searches = sum(u[6] for u in all_users)  # search_count
        total_downloads = sum(u[7] for u in all_users)  # download_count
        
        await query.edit_message_text(
            f"> 📊 *Статистика бота*\n\n"
            f"*👥 Всего пользователей:* {total_users}\n"
            f"*🟢 Активных:* {active_users}\n"
            f"*🔴 Забаненных:* {banned_users}\n"
            f"*🔍 Всего поисков:* {total_searches}\n"
            f"*📥 Всего скачиваний:* {total_downloads}\n\n"
            f"> _Обновлено в реальном времени_",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ В админку", callback_data="admin_panel")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
            ]),
            parse_mode='MarkdownV2'
        )
        return
        
    elif data == "admin_users":
        if user.id not in ADMIN_USERS and not db.is_user_admin(user.id):
            await query.answer("❌ Нет доступа")
            return
            
        all_users = db.get_all_users()
        await query.edit_message_text(
            f"> 👥 *Управление пользователями*\n\n"
            f"*Всего пользователей:* {len(all_users)}\n\n"
            f"> _Выбери пользователя для управления:_",
            reply_markup=music_bot.create_users_keyboard(all_users),
            parse_mode='MarkdownV2'
        )
        return
        
    elif data == "admin_broadcast":
        if user.id not in ADMIN_USERS and not db.is_user_admin(user.id):
            await query.answer("❌ Нет доступа")
            return
            
        await query.edit_message_text(
            "> 📢 *Рассылка сообщений*\n\n"
            "_Выбери тип рассылки:_",
            reply_markup=music_bot.create_broadcast_keyboard(),
            parse_mode='MarkdownV2'
        )
        return
        
    elif data.startswith("users_page_"):
        if user.id not in ADMIN_USERS and not db.is_user_admin(user.id):
            await query.answer("❌ Нет доступа")
            return
            
        page = int(data.split('_')[2])
        all_users = db.get_all_users()
        await query.edit_message_text(
            f"> 👥 *Управление пользователями*\n\n"
            f"*Всего пользователей:* {len(all_users)}\n\n"
            f"> _Выбери пользователя для управления:_",
            reply_markup=music_bot.create_users_keyboard(all_users, page),
            parse_mode='MarkdownV2'
        )
        return
        
    elif data.startswith("user_detail_"):
        if user.id not in ADMIN_USERS and not db.is_user_admin(user.id):
            await query.answer("❌ Нет доступа")
            return
            
        user_id = int(data.split('_')[2])
        all_users = db.get_all_users()
        target_user = next((u for u in all_users if u[0] == user_id), None)
        
        if target_user:
            user_id, username, first_name, last_name, is_banned, is_admin, search_count, download_count = target_user
            name = first_name or username or f"User {user_id}"
            status = "🔴 Забанен" if is_banned else "🟢 Активен"
            admin_status = "👑 Админ" if is_admin else "👤 Пользователь"
            
            await query.edit_message_text(
                f"> 👤 *Информация о пользователе*\n\n"
                f"*ID:* `{user_id}`\n"
                f"*Имя:* {name}\n"
                f"*Статус:* {status}\n"
                f"*Роль:* {admin_status}\n"
                f"*🔍 Поисков:* {search_count}\n"
                f"*📥 Скачиваний:* {download_count}\n\n"
                f"> _Выбери действие:_",
                reply_markup=music_bot.create_user_management_keyboard(user_id, is_banned),
                parse_mode='MarkdownV2'
            )
        return
        
    elif data.startswith("ban_"):
        if user.id not in ADMIN_USERS and not db.is_user_admin(user.id):
            await query.answer("❌ Нет доступа")
            return
            
        target_user_id = int(data.split('_')[1])
        db.ban_user(target_user_id)
        await query.answer("✅ Пользователь забанен")
        await query.edit_message_reply_markup(
            reply_markup=music_bot.create_user_management_keyboard(target_user_id, True)
        )
        return
        
    elif data.startswith("unban_"):
        if user.id not in ADMIN_USERS and not db.is_user_admin(user.id):
            await query.answer("❌ Нет доступа")
            return
            
        target_user_id = int(data.split('_')[1])
        db.unban_user(target_user_id)
        await query.answer("✅ Пользователь разбанен")
        await query.edit_message_reply_markup(
            reply_markup=music_bot.create_user_management_keyboard(target_user_id, False)
        )
        return
        
    elif data.startswith("make_admin_"):
        if user.id not in ADMIN_USERS and not db.is_user_admin(user.id):
            await query.answer("❌ Нет доступа")
            return
            
        target_user_id = int(data.split('_')[2])
        db.make_admin(target_user_id)
        await query.answer("✅ Пользователь стал админом")
        await query.edit_message_reply_markup(
            reply_markup=music_bot.create_user_management_keyboard(target_user_id, False)
        )
        return
        
    elif data.startswith("broadcast_"):
        if user.id not in ADMIN_USERS and not db.is_user_admin(user.id):
            await query.answer("❌ Нет доступа")
            return
            
        broadcast_type = data.split('_')[1]
        context.user_data['broadcast_type'] = broadcast_type
        context.user_data['waiting_for_broadcast'] = True
        
        await query.edit_message_text(
            "> 📢 *Введи сообщение для рассылки:*\n\n"
            "_Можно использовать MarkdownV2 форматирование_\n"
            "_Отправь /cancel для отмены_",
            parse_mode='MarkdownV2'
        )
        return
        
    elif data == "new_search":
        await query.edit_message_text(
            "> 🔍 *Введи название трека или артиста:*\n\n"
            "_Пример: Lana Del Radio Young_",
            parse_mode='MarkdownV2'
        )
        context.user_data['waiting_for_text_search'] = True
        return
        
    elif data.startswith("page_"):
        page = int(data.split('_')[1])
        last_query = context.user_data.get('last_query')
        if last_query:
            await perform_search(update, context, last_query, page)
        return
        
    elif data.startswith("back_"):
        page = int(data.split('_')[1])
        results = context.user_data.get('last_results', {})
        last_query = context.user_data.get('last_query', '')
        
        reply_markup = music_bot.create_search_keyboard(results, page)
        
        sources_found = []
        if results.get('deezer'):
            sources_found.append(f"ᴅᴇᴇᴢᴇʀ \\({len(results['deezer'])}\\)")
        if results.get('youtube'):
            sources_found.append(f"ʏᴏᴜᴛᴜʙᴇ \\({len(results['youtube'])}\\)")
        if results.get('soundcloud'):
            sources_found.append(f"sᴏᴜɴᴅᴄʟᴏᴜᴅ \\({len(results['soundcloud'])}\\)")
        
        sources_text = ", ".join(sources_found)
        
        response_text = (
            f"> 🎵 *Результаты для:* `{last_query}`\n"
            f"*📊 Найдено треков:* {sum(len(results.get(source, [])) for source in ['deezer', 'youtube', 'soundcloud'])}\n"
            f"*📄 Страница* {page + 1}\n\n"
            f"> *Выбери трек для скачивания:*"
        )
        
        await query.edit_message_text(response_text, reply_markup=reply_markup, parse_mode='MarkdownV2')
        return
        
    elif data.startswith("track_"):
        parts = data.split('_')
        if len(parts) >= 4:
            source = parts[1]
            track_id = parts[2].replace('-', '_')  # Возвращаем оригинальные символы
            page = int(parts[3])
            
            results = context.user_data.get('last_results', {})
            track_data = None
            
            if source in results:
                for track in results[source]:
                    if str(track['id']) == track_id:
                        track_data = track
                        break
            
            if track_data:
                message = music_bot.create_track_info_message(track_data, source)
                keyboard = music_bot.create_track_keyboard(track_data, source, track_id, page)
                await query.edit_message_text(message, reply_markup=keyboard, parse_mode='MarkdownV2')
            else:
                await query.answer("❌ Трек не найден")
        return
        
    elif data.startswith("rec_"):
        parts = data.split('_')
        if len(parts) >= 3:
            track_title = parts[1].replace('_', ' ')
            track_artist = parts[2].replace('_', ' ')
            
            track_data = {
                'title': track_title,
                'artist': track_artist,
                'album': 'Распознанный трек'
            }
            
            message = (
                f"> 🎵 *{track_data['title']}*\n\n"
                f"*👤 Артист:* {track_data['artist']}\n"
                f"*💿 Альбом:* {track_data['album']}\n"
                f"*🎼 Источник:* ʀᴀsᴘᴏᴢɴᴀɴɴᴏ\n\n"
                f"_Будет скачан полный трек с YouTube_"
            )
            
            keyboard = music_bot.create_track_keyboard(track_data=track_data)
            await query.edit_message_text(message, reply_markup=keyboard, parse_mode='MarkdownV2')
        return
        
    elif data.startswith("dl_"):
        parts = data.split('_')
        if len(parts) >= 3:
            source = parts[1]
            
            if source == "rec" and len(parts) >= 4:
                track_title = parts[2].replace('_', ' ')
                track_artist = parts[3].replace('_', ' ')
                await download_track(update, context, None, 'youtube', f"{track_title} {track_artist}")
                
            elif source == "youtube" and len(parts) >= 4:
                track_id = parts[2].replace('-', '_')
                page = int(parts[3])
                await download_track(update, context, track_id, 'youtube', page=page)
                
            elif source == "deezer" and len(parts) >= 4:
                track_id = parts[2].replace('-', '_')
                page = int(parts[3])
                await download_track(update, context, track_id, 'deezer', page=page)
                
            elif source == "soundcloud" and len(parts) >= 4:
                track_id = parts[2].replace('-', '_')
                page = int(parts[3])
                await download_track(update, context, track_id, 'soundcloud', page=page)
        
        return

async def download_track(update: Update, context: ContextTypes.DEFAULT_TYPE, track_id: str = None, source: str = None, query: str = None, page: int = 0):
    query_obj = update.callback_query
    user = query_obj.from_user
    
    try:
        if source == 'youtube':
            if track_id:
                video_url = f"https://youtu.be/{track_id}"
                download_msg = await query_obj.edit_message_text("> 📹 Скачиваю с YouTube\\.\\.\\.")
                result = await music_bot.download_youtube_audio(video_url)
            elif query:
                download_msg = await query_obj.edit_message_text("> 📹 Ищу на YouTube\\.\\.\\.")
                search_results = await music_bot.search_youtube_music(query)
                if search_results:
                    video_url = f"https://youtu.be/{search_results[0]['id']}"
                    result = await music_bot.download_youtube_audio(video_url)
                else:
                    await download_msg.edit_text("> ❌ Не найдено на YouTube")
                    return
            else:
                await query_obj.answer("❌ Ошибка: не указан трек")
                return
                
        elif source == 'soundcloud':
            results = context.user_data.get('last_results', {})
            track_data = None
            track_url = None
            
            if track_id:
                for track in results.get('soundcloud', []):
                    if str(track['id']) == track_id:
                        track_data = track
                        track_url = track.get('url')
                        break
            
            if not track_url:
                await query_obj.answer("❌ Не удалось найти трек SoundCloud")
                return
                
            download_msg = await query_obj.edit_message_text("> 🎧 Скачиваю с SoundCloud\\.\\.\\.")
            result = await music_bot.download_soundcloud_track(track_url)
            
        elif source == 'deezer':
            results = context.user_data.get('last_results', {})
            track_data = None
            
            if track_id:
                for track in results.get('deezer', []):
                    if str(track['id']) == track_id:
                        track_data = track
                        break
            
            if not track_data:
                await query_obj.answer("❌ Не удалось найти трек Deezer")
                return
                
            download_msg = await query_obj.edit_message_text("> 🎵 Скачиваю превью с Deezer\\.\\.\\.")
            result = await music_bot.download_deezer_preview(track_data)
            
        else:
            await query_obj.answer("❌ Неизвестный источник")
            return
        
        if result and os.path.exists(result['file_path']):
            # Добавляем в историю скачиваний
            if source == 'deezer' and track_data:
                db.add_download_history(user.id, track_data['title'], track_data['artist'], source)
            elif result.get('title'):
                artist = result.get('artist', 'Unknown')
                db.add_download_history(user.id, result['title'], artist, source)
            
            file_size = os.path.getsize(result['file_path'])
            
            if file_size > 50 * 1024 * 1024:  # 50MB limit for Telegram
                await download_msg.edit_text("> ❌ Файл слишком большой для отправки в Telegram")
                os.remove(result['file_path'])
                return
            
            # Отправляем файл
            with open(result['file_path'], 'rb') as audio_file:
                caption = (
                    f"🎵 *{result.get('title', 'Трек')}*\n"
                    f"👤 *Артист:* {result.get('artist', 'Неизвестно')}\n"
                    f"💿 *Альбом:* {result.get('album', 'Неизвестно')}\n"
                    f"🎼 *Источник:* {source.upper()}\n\n"
                    f"_Скачано через @{(await context.bot.get_me()).username}_"
                )
                
                # ФИКС: Используем правильный параметр для обложки
                if result.get('cover_path') and os.path.exists(result['cover_path']):
                    with open(result['cover_path'], 'rb') as cover_file:
                        await context.bot.send_audio(
                            chat_id=query_obj.message.chat_id,
                            audio=audio_file,
                            title=result.get('title', 'Audio')[:64],
                            performer=result.get('artist', 'Unknown')[:64],
                            thumbnail=cover_file,  # ФИКС: thumb -> thumbnail
                            caption=caption,
                            parse_mode='MarkdownV2'
                        )
                    os.remove(result['cover_path'])
                else:
                    await context.bot.send_audio(
                        chat_id=query_obj.message.chat_id,
                        audio=audio_file,
                        title=result.get('title', 'Audio')[:64],
                        performer=result.get('artist', 'Unknown')[:64],
                        caption=caption,
                        parse_mode='MarkdownV2'
                    )
            
            # Удаляем временные файлы
            os.remove(result['file_path'])
            
            # Обновляем сообщение
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Новый поиск", callback_data="new_search")],
                [InlineKeyboardButton("⬅️ К результатам", callback_data=f"back_{page}")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
            ])
            
            await download_msg.edit_text(
                "> ✅ *Трек успешно скачан\\!*\n\n"
                "_Что дальше?_",
                reply_markup=keyboard,
                parse_mode='MarkdownV2'
            )
            
        else:
            await download_msg.edit_text("> ❌ Ошибка при скачивании трека")
            if result and os.path.exists(result['file_path']):
                os.remove(result['file_path'])
                
    except Exception as e:
        logger.error(f"Download error: {e}")
        try:
            await query_obj.edit_message_text("> ❌ Ошибка при скачивании трека")
        except:
            pass

async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str):
    user = update.effective_user
    
    if user.id not in ADMIN_USERS and not db.is_user_admin(user.id):
        await update.message.reply_text("❌ Нет доступа")
        return
        
    broadcast_type = context.user_data.get('broadcast_type')
    all_users = db.get_all_users()
    
    if broadcast_type == 'active':
        target_users = [u for u in all_users if not u[4]]  # not banned
    else:
        target_users = all_users
    
    context.user_data['waiting_for_broadcast'] = False
    context.user_data['broadcast_type'] = None
    
    progress_msg = await update.message.reply_text("> 📢 Рассылка начата\\.\\.\\. Отправлено 0/{len(target_users)}")
    
    success_count = 0
    fail_count = 0
    
    for i, user_data in enumerate(target_users):
        user_id = user_data[0]
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=message_text,
                parse_mode='MarkdownV2'
            )
            success_count += 1
        except Exception as e:
            logger.error(f"Broadcast error for {user_id}: {e}")
            fail_count += 1
        
        if (i + 1) % 10 == 0:
            await progress_msg.edit_text(
                f"> 📢 Рассылка\\.\\.\\. Отправлено {i+1}/{len(target_users)}"
            )
        
        await asyncio.sleep(0.1)
    
    await progress_msg.edit_text(
        f"> 📢 *Рассылка завершена\\!*\n\n"
        f"*✅ Успешно:* {success_count}\n"
        f"*❌ Ошибок:* {fail_count}\n"
        f"*👥 Всего:* {len(target_users)}",
        parse_mode='MarkdownV2'
    )

async def handle_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # ФИКС: Если пользователь уже админ, сразу открываем админку
    if user.id in ADMIN_USERS or db.is_user_admin(user.id):
        if user.id not in ADMIN_USERS:
            ADMIN_USERS.append(user.id)
        keyboard = music_bot.create_admin_keyboard()
        await update.message.reply_text(
            "> 👑 *Панель администратора*\n\n*Выбери действие:*",
            reply_markup=keyboard,
            parse_mode='MarkdownV2'
        )
        return
    
    # Если не админ, просим пароль
    context.user_data['waiting_for_admin_password'] = True
    await update.message.reply_text(
        "> 🔐 *Вход в админку*\n\n"
        "_Введи пароль администратора:_\n"
        "_Отправь /cancel для отмены_",
        parse_mode='MarkdownV2'
    )

async def show_main_menu(callback_query, context: ContextTypes.DEFAULT_TYPE):
    """Показывает главное меню из callback запроса"""
    user = callback_query.from_user
    
    keyboard = [
        [InlineKeyboardButton("🔍 Поиск по названию", callback_data="text_search")],
        [InlineKeyboardButton("🎤 Распознать аудио", callback_data="recognize_audio")],
        [InlineKeyboardButton("📊 Моя статистика", callback_data="my_stats")]
    ]
    
    if user.id in ADMIN_USERS or db.is_user_admin(user.id):
        keyboard.append([InlineKeyboardButton("👑 Админка", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "> 🎵 *Главное меню*\n\n"
        "*Выбери действие:*"
    )
    
    await callback_query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode='MarkdownV2')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

def main():
    print("🎵 Музыкальный бот запускается...")
    print(f"🔑 Пароль админки: {ADMIN_PASSWORD}")
    print("🎶 Deezer API: Активен")
    print("📹 YouTube API: Активен") 
    print("🎧 SoundCloud: Активен")
    print("🚀 Улучшенная обработка ошибок!")
    print("🔧 ФИКС: Исправлена система банов!")
    
    # Инициализация бота с правильными параметрами
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", handle_admin_command))  # ФИКС: Добавлен обработчик команды /admin
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)
    
    print("✅ Бот успешно запущен! Напиши /start в Telegram")
    print("👑 Для входа в админку: /admin")
    print("🔧 Исправлены ошибки банов и добавлены обложки!")
    print("📝 Все сообщения теперь в стиле цитирования MarkdownV2!")
    
    # Запуск бота
    application.run_polling()

if __name__ == "__main__":
    main()
