import cv2 # type: ignore
import numpy as np # type: ignore
import openai
import os
import base64
import time
import threading
import queue
import torch # type: ignore
from datetime import datetime
from dotenv import load_dotenv
import sys
import warnings
import shutil
import asyncio
from discord_bot import bot, send_image
from prompt_manager import PromptManager
import subprocess
import argparse
from openai import OpenAI
import requests
from ultralytics import YOLO # type: ignore
from person_detector import PersonDetector

class RoastingMirror:
    """
    A smart mirror application that detects people using YOLOv5-tiny
    and provides AI-generated fashion critiques via the OpenAI API.
    
    This class implements a computer vision system that uses a webcam to detect people,
    captures their image, and generates both text and audio feedback about their appearance
    using OpenAI's GPT-4 Vision and text-to-speech capabilities.
    
    Attributes:
        client (openai.OpenAI): OpenAI client instance for API interactions
        tts_engine (pyttsx3.Engine): Text-to-speech engine for audio output
        camera (cv2.VideoCapture): Webcam capture device
        model (torch.hub.load): YOLOv5 model for person detection
        last_roast_time (float): Timestamp of the last generated roast
        roast_cooldown (int): Minimum time (seconds) between roasts
    """

    # Define style names as a class variable
    style_names = {
        1: "Kind & Child-Friendly",
        2: "Professional & Balanced",
        3: "Weather-Aware",
        4: "Ultra-Critical Expert",
        5: "Savage Roast Master"
    }

    def __init__(self, use_lambda=False, horizontal_mode=False):
        """
        Initialize the RoastingMirror with all necessary components
        
        Args:
            use_lambda (bool): Whether to use Lambda Labs API instead of OpenAI
            horizontal_mode (bool): Whether to use horizontal (landscape) orientation
        """
        print("[Init] Starting initialization...")
        
        # Define local model directory
        self.model_dir = os.path.join(os.path.dirname(__file__), 'models')
        os.makedirs(self.model_dir, exist_ok=True)
        
        # Store API choice and orientation mode
        self.use_lambda = use_lambda
        self.horizontal_mode = horizontal_mode
        
        # Initialize OpenAI or Lambda Labs client
        if self.use_lambda:
            self.client = OpenAI(
                api_key=os.getenv('LAMBDA_API_KEY'),
                base_url="https://api.lambdalabs.com/v1"
            )
            self.vision_model = "llama3.2-11b-vision-instruct"
        else:
            self.client = openai.OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
            self.vision_model = "gpt-4o-mini"
        
        # Initialize prompt manager and current prompt style
        self.prompt_manager = PromptManager()
        self.current_prompt_style = 1  # Default to kind, child-friendly style
        
        # Start Discord bot in a separate thread
        print("[Discord] Creating Discord bot thread...")
        self.discord_thread = threading.Thread(target=self._run_discord_bot)
        self.discord_thread.daemon = True
        self.discord_thread.start()
        print("[Discord] Discord thread started")
        
        # Wait a moment for Discord bot to initialize
        time.sleep(2)
        print("[Discord] Waited for initialization")
        
        # Load environment variables
        load_dotenv()
        
        # Suppress warnings
        warnings.filterwarnings('ignore', category=FutureWarning)
        
        # Initialize camera
        self.camera = cv2.VideoCapture(0)
        
        # Load YOLOv11 model
        try:
            print("\nLoading YOLOv11 model...")
            
            # Set up model paths
            model_name = "yolo11m.pt"
            model_path = os.path.join(self.model_dir, model_name)
            
            # Check if model exists locally
            if not os.path.exists(model_path):
                print(f"Downloading YOLOv11 model to {model_path}...")
                # Download model directly without export
                model = YOLO("yolo11m.pt")
                # Save the model to our models directory
                shutil.copy(os.path.join(os.getcwd(), model_name), model_path)
                print("Model download complete!")
            else:
                print("Found existing model in local storage")
            
            # Load the model from local path
            self.model = YOLO(model_path)
            
            # Set model parameters
            self.model.conf = 0.45  # confidence threshold
            self.model.classes = [0]  # only detect people (class 0)
            print(f"Successfully loaded YOLOv11 model!")
            
        except Exception as e:
            print(f"\nError loading YOLOv11 model: {str(e)}")
            sys.exit(1)
        
        # Initialize face detection
        cascade_path = os.path.join(cv2.data.haarcascades, 'haarcascade_frontalface_default.xml')
        self.face_cascade = cv2.CascadeClassifier(cascade_path)
        
        # Roast cooldown and tracking settings
        self.last_roast_time = 0
        self.roast_cooldown = 10  # in seconds
        self.person_present = False
        self.frame_center = None
        self.consecutive_empty_frames = 0
        self.consecutive_frames_threshold = 30  # about 1 second at 30 fps
        
        # Add audio management attributes
        self.current_audio_process = None
        self.audio_lock = threading.Lock()
        
        # Directory for saving audio files
        if not os.path.exists("sounds"):
            os.makedirs("sounds")
        # Create a temporary directory for active audio
        self.active_audio_dir = os.path.join("sounds", "active")
        if os.path.exists(self.active_audio_dir):
            shutil.rmtree(self.active_audio_dir)
        os.makedirs(self.active_audio_dir)
        
        # Threading / concurrency management
        self.roast_queue = queue.Queue()
        self.roast_in_progress = False
        self.roast_thread = None
        
        # YOLO detection parameters
        self.confidence_threshold = 0.45  # Default confidence threshold
        self.center_region_scale = 0.33  # Default center region size (1/3 of frame)
        
        # Set model parameters
        self.model.conf = self.confidence_threshold  # confidence threshold
        self.model.classes = [0]  # only detect people (class 0 in COCO dataset)
        
        # Add roast completion tracking
        self.roast_completed = True  # Track if current roast has finished
        self.skip_current_roast = False  # Flag to skip current roast
        
        # Add person tracking attributes
        self.person_count = 0  # Total number of unique people seen
        self.current_person_id = None  # ID of the person currently being tracked
        self.person_image = None  # Store the best image of current person
        self.person_center_frames = 0  # Count of frames person has been in center
        self.min_center_frames = 10  # Minimum frames in center before capturing
        
        # Initialize person detector
        self.person_detector = PersonDetector(
            model=self.model,
            confidence_threshold=0.45,
            center_region_scale=0.33
        )

    def _run_discord_bot(self):
        """
        Run the Discord bot in a separate thread
        """
        try:
            TOKEN = os.getenv('DISCORD_TOKEN')
            if not TOKEN:
                print("Error: DISCORD_TOKEN not found in .env file")
            print("[Discord] Attempting to start Discord bot...")
            print(f"[Discord] Using token: {TOKEN[:5]}...{TOKEN[-5:]}")  # Show first/last 5 chars safely
            print(f"[Discord] Bot object status: {bot}")
            asyncio.run(bot.start(TOKEN))
        except Exception as e:
            print(f"[Discord] Error starting Discord bot: {str(e)}")
            print(f"[Discord] Full error details: {repr(e)}")

    def adjust_confidence(self, delta):
        """
        Adjust the confidence threshold for YOLO detection
        
        Args:
            delta (float): Amount to adjust confidence by (positive or negative)
        """
        self.confidence_threshold = max(0.1, min(0.9, self.confidence_threshold + delta))
        self.model.conf = self.confidence_threshold
        print(f"\nConfidence threshold adjusted to: {self.confidence_threshold:.2f}")

    def adjust_center_region(self, delta):
        """
        Adjust the size of the center detection region
        
        Args:
            delta (float): Amount to adjust region scale by (positive or negative)
        """
        self.center_region_scale = max(0.1, min(0.9, self.center_region_scale + delta))
        print(f"\nCenter region scale adjusted to: {self.center_region_scale:.2f}")

    def detect_person(self, frame):
        """
        Detect and track people using YOLOv11
        """
        # Get frame dimensions
        frame_height, frame_width = frame.shape[:2]
        center_x = frame_width // 2
        center_y = frame_height // 2
        
        # Define center region
        center_region_width = int(frame_width * self.center_region_scale)
        center_region_height = int(frame_height * self.center_region_scale)
        
        # Run YOLOv11 detection
        results = self.model(frame, verbose=False)
        
        # Process detections
        detected_people = []
        
        # Convert frame to grayscale for face detection
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        person_still_in_center = False  # Track if current person is still in center
        
        for result in results[0].boxes.data:
            if int(result[5]) == 0:  # class 0 is person
                confidence = float(result[4])
                if confidence > self.confidence_threshold:
                    box = result[:4].int().tolist()
                    
                    # Calculate metrics
                    person_height = box[3] - box[1]
                    height_ratio = person_height / frame_height
                    person_center_x = (box[0] + box[2]) // 2
                    person_center_y = (box[1] + box[3]) // 2
                    
                    # Check position
                    in_center_x = abs(person_center_x - center_x) < (center_region_width // 2)
                    in_center_y = abs(person_center_y - center_y) < (center_region_height // 2)
                    
                    # Calculate foreground score (0-100%)
                    foreground_score = min(100, int((height_ratio / 0.15) * 100))
                    
                    # Check for forward-facing face
                    person_roi = gray[box[1]:box[3], box[0]:box[2]]
                    faces = self.face_cascade.detectMultiScale(person_roi, 1.1, 4)
                    facing_forward = len(faces) > 0
                    
                    # If this is our current tracked person and they're still in position
                    if (self.person_present and 
                        in_center_x and in_center_y and 
                        facing_forward and 
                        foreground_score >= 70):
                        person_still_in_center = True
                    
                    # Calculate priority score
                    priority_score = (
                        (foreground_score * 0.4) +
                        ((in_center_x and in_center_y) * 30) +
                        (facing_forward * 30)
                    )
                    
                    detected_people.append({
                        'box': box,
                        'priority_score': priority_score,
                        'foreground_score': foreground_score,
                        'facing_forward': facing_forward,
                        'in_center': in_center_x and in_center_y
                    })
        
        # Sort and limit to top 10 people
        detected_people.sort(key=lambda x: x['priority_score'], reverse=True)
        detected_people = detected_people[:10]
        
        # Draw information for each person
        for i, person in enumerate(detected_people, 1):
            box = person['box']
            color = (0, 255, 0) if person['facing_forward'] else (0, 165, 255)
            
            # Draw bounding box
            cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]), color, 2)
            
            # Draw person number and metrics
            info_text = f"#{i} | FG: {person['foreground_score']}%"
            if person['facing_forward']:
                info_text += " | READY"
            
            cv2.putText(frame, info_text, 
                        (box[0], box[1] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        
        # Update tracking logic
        if not detected_people or not person_still_in_center:
            self.consecutive_empty_frames += 1
        else:
            # Check highest priority person for roasting
            top_person = detected_people[0]
            if (top_person['in_center'] and 
                top_person['facing_forward'] and 
                top_person['foreground_score'] >= 70):
                
                if not self.person_present:
                    self.person_count += 1
                    self.current_person_id = self.person_count
                    self.person_present = True
                    self.person_image = frame.copy()
                    print(f"\n[Debug] New person #{self.current_person_id} captured")
                    return True
            
            self.consecutive_empty_frames = 0
        
        # Only reset tracking when person has actually left
        if self.consecutive_empty_frames >= self.consecutive_frames_threshold:
            if self.person_present:
                print(f"\n[Debug] Person #{self.current_person_id} has left the scene")
                self.current_person_id = None
                self.person_present = False
                self.person_image = None
                self.last_roast_time = 0  # Reset timer only when person leaves
            self.consecutive_empty_frames = 0
        
        # Draw status overlay
        status_text = f"Current: #{self.current_person_id} | " if self.current_person_id else ""
        if self.person_present:
            if person_still_in_center:
                status_text += "Person still in frame - waiting for exit"
                # Don't show cooldown timer while person is still in frame
            else:
                status_text += "Roasted - waiting for complete exit"
                # Show cooldown timer only when person has moved from center
                time_remaining = max(0, self.roast_cooldown - (time.time() - self.last_roast_time))
                cv2.putText(frame, f"Next capture in: {int(time_remaining)}s", 
                            (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        else:
            status_text += "Ready for new person"
        
        cv2.putText(frame, status_text, (10, 90), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        return False

    def _clear_audio(self):
        """
        Stop current audio playback and clear the buffer
        """
        with self.audio_lock:
            if self.current_audio_process:
                if sys.platform == "darwin":  # macOS
                    os.system("pkill afplay")
                elif sys.platform == "win32":  # Windows
                    os.system("taskkill /F /IM wmplayer.exe >NUL 2>&1")
                else:  # Linux
                    os.system("pkill -f xdg-open")
                self.current_audio_process = None
            
            # Clear the active audio directory
            for file in os.listdir(self.active_audio_dir):
                os.remove(os.path.join(self.active_audio_dir, file))

    def generate_and_play_audio(self, text):
        """
        Generate and play audio for roast text using OpenAI's text-to-speech (audio preview).
        
        Args:
            text (str): The roast text to be converted to speech
        """
        try:
            # Clear any existing audio first
            self._clear_audio()

            with self.audio_lock:
                # Note: Currently the API only supports MP3 format
                completion = self.client.chat.completions.create(
                    model="gpt-4o-mini-audio-preview",
                    modalities=["text", "audio"],
                    audio={
                        "voice": "fable",
                        # "format": "mp3",  # Currently only MP3 is supported
                        "format": "pcm16", # Future support for streaming PCM
                        # "sample_rate": 24000  # Future support for sample rate
                    },
                    messages=[
                        {
                            "role": "system",
                            "content": self.prompt_manager.get_audio_system_prompt()
                        },
                        {
                            "role": "user",
                            "content": text,
                        }
                    ],
                )
                
                # Decode PCM16 data and convert to WAV format
                pcm_bytes = base64.b64decode(completion.choices[0].message.audio.data)
                
                # Save to active audio directory instead
                speech_file_path = os.path.join(self.active_audio_dir, f"roast_{int(time.time())}.wav")
                
                import wave
                with wave.open(speech_file_path, 'wb') as wav_file:
                    wav_file.setnchannels(1)  # Mono audio
                    wav_file.setsampwidth(2)  # 2 bytes per sample for PCM16
                    wav_file.setframerate(24000)  # Sample rate
                    wav_file.writeframes(pcm_bytes)
                
                # Play audio with process tracking
                if sys.platform == "darwin":  # macOS
                    self.current_audio_process = subprocess.Popen(['afplay', speech_file_path])
                elif sys.platform == "win32":  # Windows
                    self.current_audio_process = subprocess.Popen(['start', speech_file_path], shell=True)
                else:  # Linux
                    self.current_audio_process = subprocess.Popen(['xdg-open', speech_file_path])
            
        except Exception as e:
            print(f"Error generating or playing audio: {str(e)}")
    
    def _roast_worker(self, image_data):
        """
        Background worker function to handle GPT-based roast generation 
        and then call the audio generation method.
        
        Args:
            image_data (bytes): base64-encoded, in-memory representation of the frame
        """
        try:
            self.roast_completed = False
            print("\n🎭 Generating fashion critique...\n")
            
            # Get appropriate prompts based on current style
            system_prompt = getattr(
                self.prompt_manager, 
                f'get_vision_system_prompt_{self.current_prompt_style}'
            )()
            user_prompt = getattr(
                self.prompt_manager, 
                f'get_vision_user_prompt_{self.current_prompt_style}'
            )()
            
            # Save debug image
            try:
                debug_image_path = f"debug_capture_{int(time.time())}.jpg"
                print(f"[Debug] Attempting to save debug image to: {debug_image_path}")
                image_bytes = base64.b64decode(image_data)
                with open(debug_image_path, "wb") as f:
                    f.write(image_bytes)
                print(f"[Debug] Successfully saved debug image")
            except Exception as img_error:
                print(f"[Debug] Failed to save debug image: {str(img_error)}")

            # Create API request based on provider
            if self.use_lambda:
                lambda_api_url = os.getenv('LAMBDA_API_URL')
                if not lambda_api_url:
                    raise ValueError("LAMBDA_API_URL not found in environment variables")
                
                print(f"\n[Debug] Using Lambda API URL: {lambda_api_url}")
                
                # Format the messages for Lambda's vision model
                messages = [
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": user_prompt
                            },
                            {
                                "type": "image",
                                "image": {
                                    "data": image_data
                                }
                            }
                        ]
                    }
                ]

                # Make Lambda API request
                try:
                    response = requests.post(
                        f"{lambda_api_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {os.getenv('LAMBDA_API_KEY')}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "messages": messages,
                            "model": self.vision_model,
                            "max_tokens": 500,
                            "temperature": 0.8
                        }
                    )
                    
                    print(f"\n[Debug] Lambda API Response Status: {response.status_code}")
                    print(f"[Debug] Lambda API Response Headers: {response.headers}")
                    
                    # Check if the request was successful
                    response.raise_for_status()
                    
                    # Parse the JSON response
                    response_data = response.json()
                    print(f"[Debug] Lambda API Response Data: {response_data}")
                    
                    if response_data is None:
                        raise ValueError("Received empty response from Lambda API")
                        
                    if 'choices' not in response_data:
                        raise ValueError(f"Missing 'choices' in response: {response_data}")
                        
                    if not response_data['choices']:
                        raise ValueError("Empty choices array in response")
                        
                    roast_text = response_data['choices'][0]['message']['content']
                    
                except requests.exceptions.RequestException as e:
                    raise ValueError(f"Lambda API request failed: {str(e)}")
                except (KeyError, IndexError) as e:
                    raise ValueError(f"Failed to parse Lambda API response: {str(e)}")
                
            else:
                # Original OpenAI implementation
                response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}", "detail": "high"}}
                        ]
                    }
                ],
                max_tokens=500,
                temperature=0.8

            )
            
            roast_text = response.choices[0].message.content
            
            # Print the roast with some formatting
            print("\n📺 Fashion Judge Says:")
            print("=" * 50)
            print(roast_text)
            print("=" * 50 + "\n")

            asyncio.run(send_image("•☽────✧˖°˖☆˖°˖✧────☾•" "\n" + roast_text + "\n" + "⬇️ ⬇️ ⬇️", debug_image_path))
            
            # Generate and play audio for the roast
            self.generate_and_play_audio(roast_text)
            
            # Wait for audio playback to complete or skip
            while self.current_audio_process and self.current_audio_process.poll() is None:
                if self.skip_current_roast:
                    self._clear_audio()
                    self.skip_current_roast = False
                    break
                time.sleep(0.1)
            
            self.roast_completed = True  # Mark as completed when done
            return roast_text
        except Exception as e:
            error_message = f"Error generating roast: {str(e)}"
            print(f"\n❌ {error_message}\n")
            self.roast_completed = True  # Mark as completed even on error
        finally:
            self.roast_in_progress = False

    def _start_roast_generation(self, image):
        """Start the roast generation process with the given image"""
        if image is None:
            print("No valid person image captured for roasting.")
            return
        
        print("Starting roast generation...")
        self.roast_in_progress = True
        self.roast_completed = False
        
        # Create a copy of the image for the roast thread
        roast_image = image.copy()
        
        # Start the roast generation in a new thread
        self.roast_thread = threading.Thread(
            target=self._roast_worker,
            args=(self._encode_image(roast_image),)
        )
        self.roast_thread.daemon = True
        self.roast_thread.start()

    def _encode_image(self, image):
        """
        Encode an OpenCV image to base64 string for API requests
        
        Args:
            image (numpy.ndarray): OpenCV image in BGR format
            
        Returns:
            str: Base64 encoded image string
        """
        # Convert image from BGR to RGB
        rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        
        # Encode image to JPEG format
        _, buffer = cv2.imencode('.jpg', rgb_image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        
        # Convert to base64 string
        base64_image = base64.b64encode(buffer).decode('utf-8')
        
        return base64_image

    def run(self):
        """
        Main application loop for the RoastingMirror.
        
        Continuously captures frames from the camera, detects persons,
        and generates roasts when appropriate. Handles the following:
        - Camera frame capture and mirror display
        - Person detection
        - Cooldown timer management
        - Roast generation and playback
        - User interface elements
        - Cleanup on exit
        
        The loop continues until the user presses 'q' to quit.
        """
        print("Starting Miragé - The Roasting Smart Mirror (YOLOv11 Edition)")
        print("Press 'q' to quit")
        print(f"\nOrientation: {'Horizontal' if self.horizontal_mode else 'Vertical'}")
        print("\nDetection Controls:")
        print("[ and ] - Adjust confidence threshold (currently: {:.2f})".format(self.confidence_threshold))
        print("- and + - Adjust center region size (currently: {:.2f})".format(self.center_region_scale))
        print("\nStyle Controls:")
        print("1-5 to switch between different critic styles:")
        print("1: Kind & Child-Friendly")
        print("2: Professional & Balanced")
        print("3: Weather-Aware")
        print("4: Ultra-Critical Expert")
        print("5: Savage Roast Master")
        print("\nManual Control:")
        print("SPACE - Force trigger next roast")
        print("BACKSPACE - Skip current roast")
        
        while True:
            ret, frame = self.camera.read()
            if not ret:
                print("Camera frame capture failed.")
                break
            
            # Process frame orientation
            if not self.horizontal_mode:
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
            mirror_frame = cv2.flip(frame, 1)
            
            # Process frame through person detector
            detected_people, status = self.person_detector.process_frame(mirror_frame)
            
            # Check if should trigger roast
            should_roast, person_image = self.person_detector.should_trigger_roast()
            if should_roast and not self.roast_in_progress:
                if person_image is not None:
                    print("Starting roast generation with captured image")
                    self._start_roast_generation(person_image)
                else:
                    print("No valid person image available for roasting")
            
            # Draw status overlay
            cv2.putText(mirror_frame, status, 
                        (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            
            # Show the frame
            cv2.imshow("Miragé", mirror_frame)
            
            # Handle key presses
            if not self._handle_keys():
                break
        
        self._cleanup()

    def _draw_ui(self, frame, detected_people, status):
        # Display current critic style
        cv2.putText(frame, f"Style: {self.style_names[self.current_prompt_style]}", 
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        # Display detection parameters
        cv2.putText(frame, f"Conf: {self.confidence_threshold:.2f}", 
                    (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(frame, f"Region: {self.center_region_scale:.2f}", 
                    (10, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        # Display status message
        cv2.putText(frame, status, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        
        # Show the resulting frame
        cv2.imshow("Miragé (YOLOv5)", frame)

    def _handle_keys(self):
        # Handle key presses
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            return False
        elif key == ord(' '):  # Space bar press
            if (time.time() - self.last_roast_time >= self.roast_cooldown 
                and not self.roast_in_progress 
                and self.roast_completed):  # Only trigger if previous roast completed
                print("Manual trigger activated! Generating roast...")
                self._start_roast_generation(self.person_image)
                self.last_roast_time = time.time()
            else:
                if not self.roast_completed:
                    print("Please wait for current roast to complete")
                elif not time.time() - self.last_roast_time >= self.roast_cooldown:
                    print("Please wait for cooldown to finish")
        elif key == 8:  # Backspace key
            if not self.roast_completed:
                print("Skipping current roast...")
                self.skip_current_roast = True
                self.roast_completed = True  # Mark as completed when skipping
        elif key == ord('['):
            self.adjust_confidence(-0.05)
        elif key == ord(']'):
            self.adjust_confidence(0.05)
        elif key == ord('-'):
            self.adjust_center_region(-0.05)
        elif key == ord('=') or key == ord('+'):  # Both - and = keys work
            self.adjust_center_region(0.05)
        elif ord('1') <= key <= ord('5'):
            self.current_prompt_style = key - ord('0')
            print(f"\nSwitched to style: {self.style_names[self.current_prompt_style]}")
        elif key == ord('c'):
            self._clear_audio()
            print("\nCleared audio playback")
        
        return True

    def _cleanup(self):
        # Cleanup on exit
        self._clear_audio()  # Clear audio before closing
        self.camera.release()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    # Add argument parser
    parser = argparse.ArgumentParser(description='Miragé - The Roasting Smart Mirror')
    parser.add_argument('--lambda', action='store_true', help='Use Lambda Labs API instead of OpenAI')
    parser.add_argument('--hori', action='store_true', help='Use horizontal (landscape) orientation')
    args = parser.parse_args()
    
    # Load environment variables
    load_dotenv()
    
    # Verify all required environment variables
    required_vars = {
        'OPENAI_API_KEY': 'OpenAI API key',
        'DISCORD_TOKEN': 'Discord bot token',
        'DISCORD_CHANNEL_ID': 'Discord channel ID'
    }
    
    # Add Lambda Labs API key requirement if --lambda is used
    if getattr(args, 'lambda'):
        required_vars['LAMBDA_API_KEY'] = 'Lambda Labs API key'
    
    missing_vars = []
    for var, name in required_vars.items():
        if not os.getenv(var):
            missing_vars.append(name)
    
    if missing_vars:
        print("Missing required environment variables:")
        for var in missing_vars:
            print(f"- {var}")
        print(f"\nPlease add them to your .env file in: {os.getcwd()}")
        sys.exit(1)
    
    print("Starting Miragé with integrated Discord bot...")
    mirror = RoastingMirror(
        use_lambda=getattr(args, 'lambda'),
        horizontal_mode=getattr(args, 'hori')
    )
    mirror.run()    