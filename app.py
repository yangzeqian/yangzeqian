import os
import json
import qrcode
import netifaces
import asyncio
import websockets
from datetime import datetime
import math
from threading import Lock
import shutil 

# 导入 Flask 相关的模块
from flask import Flask, request, jsonify, send_from_directory, render_template
from werkzeug.utils import secure_filename # Added for general use

# --- Flask 应用初始化 ---
app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- 全局变量和初始化 ---
# 增加一个全局锁，用于保护对JSON文件的读写
DATA_LOCK = Lock()

MESSAGES_FILE = 'messages.json'
URLS_FILE = 'urls.json'
FILES_METADATA_FILE = 'files_metadata.json' # New metadata file for files

def ensure_data_files():
    """确保JSON数据文件存在"""
    os.makedirs(UPLOAD_FOLDER, exist_ok=True) # 确保上传目录存在
    for filename in [MESSAGES_FILE, URLS_FILE]: # These are lists
        if not os.path.exists(filename):
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump([], f) # 初始化为空列表
            print(f"[Init] Created empty {filename}")
    
    # New: Ensure files_metadata.json exists and is a dict
    if not os.path.exists(FILES_METADATA_FILE):
        with open(FILES_METADATA_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f) # 初始化为空字典
        print(f"[Init] Created empty {FILES_METADATA_FILE}")


def get_local_ip():
    """获取本机在局域网中的IP地址"""
    try:
        # 尝试获取默认网关接口的IP
        gws = netifaces.gateways()
        default_interface = gws['default'][netifaces.AF_INET][1]
        if_addrs = netifaces.ifaddresses(default_interface)
        ip = if_addrs[netifaces.AF_INET][0]['addr']
        return ip
    except Exception:
        # 如果上述方法失败，尝试更通用的方式
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # doesn't connect to the actual network, just finds local interface
            s.connect(('10.255.255.255', 1))
            IP = s.getsockname()[0]
        except Exception:
            IP = '127.0.0.1' # Fallback to localhost
        finally:
            s.close()
        return IP

# --- 新增和修改的辅助函数 ---

def format_file_size(size_bytes):
    """将字节数格式化为易读的字符串 (如 1.2 MB)"""
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

# 修改数据加载/保存函数以使用锁，确保并发安全
def load_data(file_path): # For lists (messages, urls)
    with DATA_LOCK: # 获取锁
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return [] # 文件不存在或内容为空/损坏时返回空列表

def save_data(file_path, data): # For lists (messages, urls)
    with DATA_LOCK: # 获取锁
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4) # 写入数据，不转义中文，美化格式
        print(f"[Data Save] Data saved to {file_path}")

# New: For dictionary-based data (files_metadata)
def load_json_dict(file_path):
    with DATA_LOCK:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {} # File not found or empty/corrupt, return empty dict

def save_json_dict(file_path, data):
    with DATA_LOCK:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        print(f"[Data Save] Data saved to {file_path}")

# --- Flask Routes ---

@app.route('/')
def index():
    """渲染主页面并传入服务器IP"""
    server_ip = get_local_ip()
    print(f"[Flask] Server IP: {server_ip}")
    return render_template('index.html', server_ip=server_ip)

@app.route('/qr')
def qr_code():
    """生成并返回当前服务器地址的二维码"""
    server_ip = get_local_ip()
    url = f"http://{server_ip}:5000"
    img = qrcode.make(url)
    img_path = os.path.join(app.config['UPLOAD_FOLDER'], 'qrcode.png')
    img.save(img_path)
    return send_from_directory(app.config['UPLOAD_FOLDER'], 'qrcode.png')

@app.route('/files')
def list_files():
    """列出uploads目录下的所有文件和文件夹，并包含文件大小、上传时间、下载量"""
    print("[Flask Route] Received request to list files.")
    items = []
    upload_folder_path = app.config['UPLOAD_FOLDER']
    
    files_metadata = load_json_dict(FILES_METADATA_FILE)

    # Ensure uploads directory exists
    if not os.path.exists(upload_folder_path):
        return jsonify([])

    # Get current top-level files (not including subfolders)
    current_top_level_files = set(
        f for f in os.listdir(upload_folder_path) if os.path.isfile(os.path.join(upload_folder_path, f))
    )

    # Cleanup metadata for files that no longer exist as top-level files
    metadata_keys_to_remove = []
    for filename_in_meta in files_metadata.keys():
        if filename_in_meta not in current_top_level_files:
            metadata_keys_to_remove.append(filename_in_meta)
    for key in metadata_keys_to_remove:
        del files_metadata[key]
    if metadata_keys_to_remove: # Only save if changes were made
        save_json_dict(FILES_METADATA_FILE, files_metadata)

    for item_name in os.listdir(upload_folder_path):
        if item_name == 'qrcode.png': # Exclude QR code file
            continue

        item_path = os.path.join(upload_folder_path, item_name)
        
        if os.path.isfile(item_path):
            try:
                size_bytes = os.path.getsize(item_path)
                
                # Get or initialize metadata
                metadata = files_metadata.get(item_name, {})
                
                upload_time = metadata.get('upload_time')
                download_count = metadata.get('download_count', 0)

                # If file exists but no metadata, or metadata is incomplete, initialize/update it
                if upload_time is None: 
                    upload_time = datetime.now().isoformat()
                    files_metadata[item_name] = {
                        'upload_time': upload_time,
                        'download_count': download_count
                    }
                    save_json_dict(FILES_METADATA_FILE, files_metadata) # Save changes

                items.append({
                    'name': item_name,
                    'type': 'file',
                    'formatted_size': format_file_size(size_bytes),
                    'upload_time': upload_time, # ISO format
                    'formatted_upload_time': datetime.fromisoformat(upload_time).strftime('%Y-%m-%d %H:%M:%S'),
                    'download_count': download_count
                })
            except OSError as e:
                print(f"[Flask Warning] Could not get info for '{item_name}': {e}. Skipping.")
                continue
        elif os.path.isdir(item_path):
            items.append({'name': item_name, 'type': 'folder', 'formatted_size': ''})
    
    print(f"[Flask Info] Listing {len(items)} items.")
    return jsonify(items)

@app.route('/download/<path:filename>')
async def download_file(filename):
    """提供文件下载功能，并增加下载计数"""
    safe_filename = secure_filename(filename) 

    item_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)

    if not os.path.exists(item_path) or not os.path.isfile(item_path):
        return jsonify({'error': '文件未找到。'}), 404

    files_metadata = load_json_dict(FILES_METADATA_FILE)
    if safe_filename in files_metadata:
        files_metadata[safe_filename]['download_count'] = files_metadata[safe_filename].get('download_count', 0) + 1
        save_json_dict(FILES_METADATA_FILE, files_metadata)
        print(f"[Download] Incremented download count for '{safe_filename}' to {files_metadata[safe_filename]['download_count']}")
    else:
        # If somehow file exists but no metadata, add it with initial count
        files_metadata[safe_filename] = {
            'upload_time': datetime.now().isoformat(),
            'download_count': 1
        }
        save_json_dict(FILES_METADATA_FILE, files_metadata)
        print(f"[Download] Added metadata for '{safe_filename}' with count 1.")

    response = send_from_directory(app.config['UPLOAD_FOLDER'], safe_filename, as_attachment=True)
    
    # Schedule the notification. This assumes an async Flask setup or external handling.
    # If Flask is run using `app.run()` directly, this `await` will raise a RuntimeError.
    # For a robust solution with Flask and separate WS, consider Redis Pub/Sub or Flask-SocketIO.
    asyncio.create_task(notify_clients(json.dumps({'type': 'file_update'}))) 
    
    return response


@app.route('/upload', methods=['POST'])
async def upload_file():
    """处理文件上传"""
    print("[Flask Route] Received upload request.")
    if 'file' not in request.files:
        return jsonify({'error': '未选择文件。'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '未选择文件。'}), 400
    if file:
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        if os.path.exists(file_path):
            return jsonify({'error': f'文件 "{filename}" 已存在，请重命名后上传。'}), 409

        try:
            file.save(file_path)
            
            # Add file metadata
            files_metadata = load_json_dict(FILES_METADATA_FILE)
            files_metadata[filename] = {
                'upload_time': datetime.now().isoformat(),
                'download_count': 0
            }
            save_json_dict(FILES_METADATA_FILE, files_metadata)

            print(f"[Flask Success] File '{filename}' uploaded successfully.")
            await notify_clients(json.dumps({'type': 'file_update'})) # 通知所有客户端文件列表更新
            return jsonify({'success': f'文件 "{filename}" 上传成功。'})
        except Exception as e:
            print(f"[Flask Error] Error saving file: {e}")
            return jsonify({'error': f'文件上传失败: {str(e)}'}), 500

@app.route('/api/folders', methods=['POST'])
async def create_folder():
    """创建新文件夹"""
    print("[Flask Route] Received create folder request.")
    data = request.get_json()
    folder_name = data.get('name', '').strip()

    if not folder_name:
        return jsonify({'error': '文件夹名称不能为空。'}), 400

    safe_folder_name = secure_filename(folder_name)
    folder_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_folder_name)

    if os.path.exists(folder_path):
        return jsonify({'error': f'文件夹 "{safe_folder_name}" 已存在。'}), 409

    try:
        os.makedirs(folder_path, exist_ok=True)
        print(f"[Flask Success] Folder '{safe_folder_name}' created successfully.")
        await notify_clients(json.dumps({'type': 'file_update'}))
        return jsonify({'success': f'文件夹 "{safe_folder_name}" 创建成功。'})
    except Exception as e:
        print(f"[Flask Error] Error creating folder: {e}")
        return jsonify({'error': f'创建文件夹失败: {str(e)}'}), 500

@app.route('/api/move_item', methods=['POST'])
async def move_item():
    """移动文件或文件夹"""
    print("[Flask Route] Received request to move item.")
    data = request.get_json()
    item_name = data.get('item_name', '').strip()
    destination_folder = data.get('destination_folder', '').strip()

    if not item_name or not destination_folder:
        return jsonify({'error': '项目名称和目标文件夹不能为空。'}), 400

    safe_item_name = secure_filename(item_name) 
    safe_destination_folder = secure_filename(destination_folder) 

    source_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_item_name)
    destination_folder_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_destination_folder)
    final_destination_path = os.path.join(destination_folder_path, safe_item_name)

    if os.path.isdir(source_path):
        if os.path.commonpath([source_path, final_destination_path]) == source_path:
            return jsonify({'error': '不能将文件夹移动到其自身或其子文件夹中。'}), 400

    print(f"[Flask Info] Attempting to move '{source_path}' to '{final_destination_path}'")

    if not os.path.exists(source_path):
        print(f"[Flask Error] Source item '{safe_item_name}' not found.")
        return jsonify({'error': '源文件或文件夹未找到。'}), 404

    if not os.path.isdir(destination_folder_path):
        print(f"[Flask Error] Destination folder '{safe_destination_folder}' does not exist or is not a directory.")
        return jsonify({'error': '目标文件夹不存在或不是一个目录。'}), 404

    if os.path.exists(final_destination_path):
        print(f"[Flask Warning] Item '{safe_item_name}' already exists in destination folder.")
        return jsonify({'error': '目标位置已存在同名文件或文件夹。'}), 409

    try:
        shutil.move(source_path, final_destination_path)
        
        # If it was a file directly in UPLOAD_FOLDER, remove its metadata as it's now nested
        # This assumes files_metadata.json only tracks top-level files.
        if os.path.isfile(final_destination_path) and not os.path.commonpath([source_path]) == os.path.commonpath([final_destination_path]):
             # This check ensures it's actually moved *out* of the tracked top-level, not just renamed in place.
            files_metadata = load_json_dict(FILES_METADATA_FILE)
            if safe_item_name in files_metadata:
                del files_metadata[safe_item_name]
                save_json_dict(FILES_METADATA_FILE, files_metadata)
                print(f"[Metadata] Removed metadata for '{safe_item_name}' as it's no longer top-level.")

        print(f"[Flask Success] Item '{safe_item_name}' moved to '{safe_destination_folder}' successfully.")
        await notify_clients(json.dumps({'type': 'file_update'}))
        return jsonify({'success': f'"{safe_item_name}" 已成功移动到 "{safe_destination_folder}"。'})
    except Exception as e:
        print(f"[Flask Error] Error moving item: {e}")
        return jsonify({'error': f'移动失败: {str(e)}'}), 500

@app.route('/delete/<path:item_name>', methods=['DELETE'])
async def delete_item(item_name):
    """删除文件或文件夹"""
    print(f"[Flask Route] Received delete request for '{item_name}'.")
    safe_item_name = secure_filename(item_name) 
    item_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_item_name)

    if not os.path.exists(item_path):
        return jsonify({'error': '文件或文件夹未找到。'}), 404

    try:
        if os.path.isfile(item_path):
            os.remove(item_path)
            # Remove file metadata
            files_metadata = load_json_dict(FILES_METADATA_FILE)
            if safe_item_name in files_metadata:
                del files_metadata[safe_item_name]
                save_json_dict(FILES_METADATA_FILE, files_metadata)
                print(f"[Metadata] Removed metadata for deleted file '{safe_item_name}'.")

            print(f"[Flask Success] File '{safe_item_name}' deleted successfully.")
            await notify_clients(json.dumps({'type': 'file_update'}))
            return jsonify({'success': f'文件 "{safe_item_name}" 已删除。'})
        elif os.path.isdir(item_path):
            shutil.rmtree(item_path)
            # No metadata for folders in current design's files_metadata.json needed
            print(f"[Flask Success] Folder '{safe_item_name}' and its contents deleted successfully.")
            await notify_clients(json.dumps({'type': 'file_update'}))
            return jsonify({'success': f'文件夹 "{safe_item_name}" 已删除。'})
    except Exception as e:
        print(f"[Flask Error] Error deleting '{safe_item_name}': {e}")
        return jsonify({'error': f'删除失败: {str(e)}'}), 500

@app.route('/api/messages', methods=['GET'])
def get_messages():
    """获取所有留言"""
    messages = load_data(MESSAGES_FILE)
    return jsonify(messages)

@app.route('/api/messages', methods=['POST'])
async def add_message():
    """添加新留言"""
    data = request.get_json()
    sender = data.get('sender', '匿名用户').strip()
    content = data.get('content', '').strip()

    if not content:
        return jsonify({'error': '留言内容不能为空。'}), 400

    messages = load_data(MESSAGES_FILE)
    # Generate ID based on current messages and ensure it's unique enough (simple counter)
    new_id = 1
    if messages:
        # Find max ID and add 1, or just len+1 if simple list
        max_id = max((msg.get('id', 0) for msg in messages), default=0)
        new_id = max_id + 1

    new_message = {
        'id': new_id,
        'sender': sender,
        'content': content,
        'timestamp': datetime.now().isoformat(),
        'formatted_timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    messages.append(new_message)
    save_data(MESSAGES_FILE, messages)

    await notify_clients(json.dumps({'type': 'message_board_update'}))
    return jsonify({'success': '留言已发表！'})

@app.route('/api/messages/<int:message_id>', methods=['DELETE'])
async def delete_message(message_id):
    """删除指定ID的留言"""
    messages = load_data(MESSAGES_FILE)
    original_len = len(messages)
    messages = [msg for msg in messages if msg['id'] != message_id]
    if len(messages) < original_len:
        save_data(MESSAGES_FILE, messages)
        await notify_clients(json.dumps({'type': 'message_board_update'}))
        return jsonify({'success': '留言已删除。'})
    return jsonify({'error': '留言未找到。'}), 404

@app.route('/api/urls', methods=['GET'])
def get_urls():
    """获取所有分享的网址"""
    urls = load_data(URLS_FILE)
    return jsonify(urls)

@app.route('/api/urls', methods=['POST'])
async def add_url():
    """添加新的网址分享"""
    data = request.get_json()
    url = data.get('url', '').strip()
    title = data.get('title', '').strip() or url

    if not url:
        return jsonify({'error': '网址不能为空。'}), 400

    if not (url.startswith('http://') or url.startswith('https://')):
        url = 'http://' + url

    urls = load_data(URLS_FILE)
    new_id = 1
    if urls:
        max_id = max((url_item.get('id', 0) for url_item in urls), default=0)
        new_id = max_id + 1

    new_url_item = {
        'id': new_id,
        'url': url,
        'title': title,
        'timestamp': datetime.now().isoformat(),
        'formatted_timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    urls.append(new_url_item)
    save_data(URLS_FILE, urls)

    await notify_clients(json.dumps({'type': 'url_update'}))
    return jsonify({'success': '网址已分享！'})

@app.route('/api/urls/<int:url_id>', methods=['DELETE'])
async def delete_url(url_id):
    """删除指定ID的网址"""
    urls = load_data(URLS_FILE)
    original_len = len(urls)
    urls = [url for url in urls if url['id'] != url_id]
    if len(urls) < original_len:
        save_data(URLS_FILE, urls)
        await notify_clients(json.dumps({'type': 'url_update'}))
        return jsonify({'success': '网址已删除。'})
    return jsonify({'error': '网址未找到。'}), 404

# --- WebSocket Server ---
CONNECTED_CLIENTS = set()

async def websocket_handler(websocket, path=None): # Changed signature to handle `path` optionally
    """处理WebSocket连接和消息"""
    print(f"[WS Debug] WebSocket connection opened. Path: {path}") # Debug print
    CONNECTED_CLIENTS.add(websocket)
    try:
        nickname = "匿名用户" # Default until 'join' message
        # Get remote address for better logging
        client_address = websocket.remote_address
        print(f"[WS Info] Client {client_address} connected. Path: {path}")

        async for message in websocket:
            data = json.loads(message)
            if data['type'] == 'join':
                nickname = data.get('nickname', '匿名用户')
                print(f"[WS] {nickname} ({client_address}) joined.")
                await broadcast(json.dumps({'type': 'notification', 'message': f'{nickname} 进入了聊天室。'}))
            elif data['type'] == 'message':
                chat_message = data['message']
                print(f"[WS] Message from {nickname} ({client_address}): {chat_message}")
                await broadcast(json.dumps({'type': 'message', 'sender': nickname, 'message': chat_message}))
    except websockets.exceptions.ConnectionClosedOK:
        print(f"[WS] Connection closed by {nickname} ({client_address}).")
    except Exception as e:
        print(f"[WS Error] WebSocket handler error for {nickname} ({client_address}): {e}") 
    finally:
        CONNECTED_CLIENTS.discard(websocket)
        # Only broadcast "left chat" if they actually sent a "join" message
        if nickname != "匿名用户":
            await broadcast(json.dumps({'type': 'notification', 'message': f'{nickname} 离开了聊天室。'}))

async def broadcast(message):
    """向所有连接的客户端广播消息"""
    if CONNECTED_CLIENTS:
        disconnected_clients = set()
        for client in list(CONNECTED_CLIENTS): # Iterate over a copy to allow modification
            try:
                await client.send(message)
            except websockets.exceptions.ConnectionClosedOK:
                print(f"[WS] Client {client.remote_address} was already closed during broadcast.")
                disconnected_clients.add(client)
            except Exception as e:
                print(f"[WS Error] Error sending to client {client.remote_address}: {e}")
                disconnected_clients.add(client)
        CONNECTED_CLIENTS.difference_update(disconnected_clients)


async def notify_clients(message):
    """发送更新通知给所有客户端 (intended for Flask to trigger WebSocket updates)"""
    try:
        # This function is designed to be called from Flask routes.
        # If Flask and WebSocket are run as separate processes (as implied by `sys.argv` main block),
        # direct `await broadcast(message)` will not work as they have separate event loops.
        # This part of the code would require inter-process communication (e.g., Redis Pub/Sub).
        # However, keeping it as is to match the original structure, assuming the user's
        # environment somehow allows this (e.g., a specific async WSGI server like gunicorn with geventwebsocket).
        await broadcast(message)
    except Exception as e:
        print(f"[Notifier Error] Could not notify clients: {e}")


# --- Main Application Logic ---
# Global variable to hold the WebSocket server instance for graceful shutdown if needed
websocket_server = None

async def main_flask():
    """运行Flask HTTP服务器"""
    print(f"\n[Flask] Starting Flask HTTP server on http://{get_local_ip()}:5000")
    # This `app.run()` is blocking and does not natively integrate with asyncio's loop.
    # If this is run in a separate process from `main_websocket`, then `notify_clients`
    # will attempt to `await` in a non-asyncio-loop context, or to an unavailable WebSocket set.
    # For a fully integrated async app, consider Quart or Flask-SocketIO.
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

async def main_websocket():
    """运行WebSocket服务器"""
    global websocket_server
    print(f"[WS] Starting WebSocket server on ws://{get_local_ip()}:8765")
    # Store the server instance for potential later management (e.g., shutdown)
    websocket_server = await websockets.serve(websocket_handler, "0.0.0.0", 8765)
    await websocket_server.wait_closed() # Keep server running forever until explicitly stopped

if __name__ == '__main__':
    import sys
    ensure_data_files() # 确保数据文件存在

    # Handle the specific execution logic from the user
    if len(sys.argv) > 1:
        if sys.argv[1] == 'flask':
            asyncio.run(main_flask())
        elif sys.argv[1] == 'websocket':
            asyncio.run(main_websocket())
        else:
            print("Usage: python app.py [flask|websocket]")
            sys.exit(1)
    else:
        print("Please specify which server to run: 'flask' or 'websocket'")
        print("Example: python app.py flask")
        print("         python app.py websocket")
        sys.exit(1)