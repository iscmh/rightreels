import os
import random
import numpy as np
import moviepy.editor as mp
from moviepy.video.fx.all import colorx
from PIL import Image
import traceback
from flask import Flask, request, redirect, url_for, send_from_directory, render_template, jsonify, send_file, session
import zipfile
import shutil
import time
from threading import Thread
from functools import wraps
import json
import concurrent.futures
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)

UPLOAD_FOLDER = 'uploads'
PROCESSED_FOLDER = 'processed'
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi'}

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['PROCESSED_FOLDER'] = PROCESSED_FOLDER
app.secret_key = 'your_secret_key_here'  # Change this to a secure random string

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
if not os.path.exists(PROCESSED_FOLDER):
    os.makedirs(PROCESSED_FOLDER)

# Global variable to store processing progress
processing_progress = {}

def load_users():
    try:
        with open('users.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_users(users):
    with open('users.json', 'w') as f:
        json.dump(users, f)

def initialize_users():
    if not users:
        users['user1'] = {'password': 'password1', 'credits': 100}
        users['user2'] = {'password': 'password2', 'credits': 50}
        save_users(users)

users = load_users()
initialize_users()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def apply_filter(clip, factor):
    return colorx(clip, factor)

def random_metadata_change():
    return {
        "artist": f"Artist{random.randint(1000, 9999)}",
        "comment": f"Comment{random.randint(1000, 9999)}",
    }

def crop_instagram_clip(clip, target_height):
    return clip.crop(y1=0, y2=target_height)

def custom_resize(clip, newsize):
    def resize_frame(frame):
        img = Image.fromarray(frame)
        return np.array(img.resize(newsize, Image.LANCZOS))
    
    return clip.fl_image(resize_frame)

def combine_videos(instagram_video_path, youtube_video_path, output_path, duration, youtube_start_time):
    logging.debug(f"Combining videos: {instagram_video_path}, {youtube_video_path}")
    try:
        insta_clip = mp.VideoFileClip(instagram_video_path).subclip(0, duration)
        yt_clip = mp.VideoFileClip(youtube_video_path)

        yt_clip = yt_clip.subclip(youtube_start_time, youtube_start_time + duration).without_audio()

        final_width = 1080  # Increased resolution
        final_height = 1920  # 16:9 aspect ratio

        insta_height = int(final_height * 0.7)
        yt_height = final_height - insta_height

        insta_clip = crop_instagram_clip(insta_clip, insta_height)
        insta_clip = custom_resize(insta_clip, (final_width, insta_height))
        yt_clip = custom_resize(yt_clip, (final_width, yt_height))

        insta_clip = insta_clip.set_position(("center", "top"))
        yt_clip = yt_clip.set_position(("center", "bottom"))

        final_clip = mp.CompositeVideoClip([insta_clip, yt_clip], size=(final_width, final_height))

        final_clip.write_videofile(output_path, codec="libx264", audio_codec="aac", threads=4, preset='medium', bitrate="5000k", fps=30)  # Increased quality settings
        logging.debug(f"Video combined and saved to: {output_path}")
    except Exception as e:
        logging.error(f"Error combining videos: {str(e)}\n{traceback.format_exc()}")
        raise
    finally:
        if 'insta_clip' in locals():
            insta_clip.close()
        if 'yt_clip' in locals():
            yt_clip.close()

def process_video(video_path, output_path, factor):
    logging.debug(f"Processing video: {video_path}")
    clip = mp.VideoFileClip(video_path)
    try:
        clip = apply_filter(clip, factor)
        clip.write_videofile(output_path, codec="libx264", audio_codec="aac", threads=4, preset='medium', bitrate="5000k", fps=30)  # Increased quality settings
        logging.debug(f"Video processed and saved to: {output_path}")
    except Exception as e:
        logging.error(f"Error processing video: {str(e)}")
    finally:
        clip.close()

def process_single_video(input_instagram_video_path, youtube_video_path, instagram_video_duration, youtube_start_time, index, task_id):
    try:
        combined_video_path = os.path.join(UPLOAD_FOLDER, f'combined_video_{index}.mp4')
        logging.debug(f"Combining videos for video {index+1}")
        combine_videos(input_instagram_video_path, youtube_video_path, combined_video_path, instagram_video_duration, youtube_start_time)
        
        if not os.path.exists(combined_video_path):
            raise FileNotFoundError(f"Combined video file not created: {combined_video_path}")
        
        output_path = os.path.join(PROCESSED_FOLDER, f'processed_video_{index}.mp4')
        logging.debug(f"Processing combined video {index+1}")
        process_video(combined_video_path, output_path, 1.1)
        
        if not os.path.exists(output_path):
            raise FileNotFoundError(f"Processed video file not created: {output_path}")

        metadata = random_metadata_change()
        with open(output_path, 'rb') as f:
            video_data = f.read()
        video_data += f"{metadata}".encode('utf-8')
        with open(output_path, 'wb') as f:
            f.write(video_data)
        
        os.remove(combined_video_path)
        
        processing_progress[task_id]['progress'] = (index + 1) / processing_progress[task_id]['total_videos'] * 100
        
        logging.debug(f"Successfully processed video {index+1}")
        return f"Processed video {index+1} successfully", output_path
    except Exception as e:
        logging.error(f"Error processing video {index+1}: {str(e)}\n{traceback.format_exc()}")
        return f"Error processing video {index+1}: {str(e)}", None

def process_videos_task(task_id, insta_filename, yt_filename, num_videos, username):
    logging.debug(f"Starting video processing task: {task_id}")
    processing_progress[task_id] = {'progress': 0, 'total_videos': num_videos}

    with mp.VideoFileClip(os.path.join(app.config['UPLOAD_FOLDER'], insta_filename)) as clip:
        instagram_video_duration = clip.duration

    with mp.VideoFileClip(os.path.join(app.config['UPLOAD_FOLDER'], yt_filename)) as clip:
        youtube_total_duration = clip.duration

    youtube_start_time = 0
    processed_videos = []

    for i in range(num_videos):
        message, processed_video_path = process_single_video(
            os.path.join(app.config['UPLOAD_FOLDER'], insta_filename),
            os.path.join(app.config['UPLOAD_FOLDER'], yt_filename),
            instagram_video_duration,
            youtube_start_time + i * instagram_video_duration,
            i,
            task_id
        )
        if processed_video_path:
            processed_videos.append(processed_video_path)
        processing_progress[task_id]['progress'] = ((i + 1) / num_videos) * 100
        
    # Update user credits after processing
    users[username]['credits'] -= num_videos
    save_users(users)

    logging.debug(f"Processed videos: {processed_videos}")
    logging.debug(f"Files in PROCESSED_FOLDER: {os.listdir(app.config['PROCESSED_FOLDER'])}")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form['password']
        for username, user_data in users.items():
            if user_data['password'] == password:
                session['user'] = username
                return redirect(url_for('upload_form'))
        return render_template('login.html', error='Invalid password')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def upload_form():
    username = session.get('user')
    if username not in users:
        session.pop('user', None)
        return redirect(url_for('login'))
    user = users[username]
    return render_template('upload.html', credits=user['credits'])

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    username = session.get('user')
    if username not in users:
        session.pop('user', None)
        return jsonify({'status': 'fail', 'message': 'User not found'})
    
    user = users[username]
    if user['credits'] <= 0:
        return jsonify({'status': 'fail', 'message': 'No credits left'})

    if 'instagram_video' not in request.files or 'youtube_video' not in request.files or 'num_videos' not in request.form:
        return jsonify({'status': 'fail', 'message': 'Missing required files or information'})

    insta_file = request.files['instagram_video']
    yt_file = request.files['youtube_video']
    num_videos = int(request.form['num_videos'])

    if num_videos > user['credits']:
        return jsonify({'status': 'fail', 'message': 'Not enough credits'})

    if insta_file and allowed_file(insta_file.filename) and yt_file and allowed_file(yt_file.filename):
        insta_filename = 'input_instagram_video.mp4'
        yt_filename = 'youtube_video.mp4'

        insta_file.save(os.path.join(app.config['UPLOAD_FOLDER'], insta_filename))
        yt_file.save(os.path.join(app.config['UPLOAD_FOLDER'], yt_filename))

        task_id = str(time.time())
        thread = Thread(target=process_videos_task, args=(task_id, insta_filename, yt_filename, num_videos, username))
        thread.start()

        return jsonify({'status': 'success', 'task_id': task_id})
    return jsonify({'status': 'fail', 'message': 'Invalid file format'})

@app.route('/progress/<task_id>')
def get_progress(task_id):
    progress = processing_progress.get(task_id, {}).get('progress', 0)
    return jsonify({'progress': progress})

@app.route('/processed')
@login_required
def show_processed_videos():
    files = [f for f in os.listdir(app.config['PROCESSED_FOLDER']) if f.endswith('.mp4')]
    logging.debug(f"Files found in processed folder: {files}")
    return render_template('processed.html', files=files)

@app.route('/processed/<filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config['PROCESSED_FOLDER'], filename)

@app.route('/download_all')
@login_required
def download_all():
    zip_path = os.path.join(app.config['PROCESSED_FOLDER'], 'all_videos.zip')
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for root, dirs, files in os.walk(app.config['PROCESSED_FOLDER']):
            for file in files:
                if file.endswith('.mp4'):
                    zipf.write(os.path.join(root, file), arcname=file)
    return send_file(zip_path, as_attachment=True, download_name='all_videos.zip')

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    return render_template('500.html'), 500

if __name__ == "__main__":
    app.run(debug=True)