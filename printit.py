import streamlit as st
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin, ImageOps
import requests
import io
import glob
import base64
import hashlib
import os
import re
import tempfile
from datetime import datetime
import time
import qrcode
from brother_ql.models import ModelsManager
from brother_ql.backends import backend_factory
from brother_ql.raster import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from brother_ql import labels  # Import the labels module
import usb.core
import subprocess
from job_queue import print_queue  # Import from renamed file

# After the imports, before the functions
if 'label_type' not in st.session_state:
    st.session_state.label_type = None
    st.session_state.label_status = None

def find_and_parse_printer():
    """Find and parse Brother QL printer information."""
    model_manager = ModelsManager()

    # Debug print to show we're searching
    print("Searching for Brother QL printer...")

    for backend_name in ["pyusb", "linux_kernel"]:
        try:
            print(f"Trying backend: {backend_name}")
            backend = backend_factory(backend_name)
            available_devices = backend["list_available_devices"]()
            print(f"Found {len(available_devices)} devices with {backend_name} backend")

            for printer in available_devices:
                print(f"Found device: {printer}")
                identifier = printer["identifier"]
                parts = identifier.split("/")

                if len(parts) < 4:
                    print(f"Skipping device with invalid identifier format: {identifier}")
                    continue

                protocol = parts[0]
                device_info = parts[2]
                serial_number = parts[3]

                try:
                    vendor_id, product_id = device_info.split(":")
                except ValueError:
                    print(f"Invalid device info format: {device_info}")
                    continue

                # Default model
                model = "QL-570"

                # Try to match product ID to determine actual model
                try:
                    product_id_int = int(product_id, 16)
                    for m in model_manager.iter_elements():
                        if m.product_id == product_id_int:
                            model = m.identifier
                            break
                    print(f"Matched printer model: {model}")
                except ValueError:
                    print(f"Invalid product ID format: {product_id}")
                    continue

                printer_info = {
                    "identifier": identifier,
                    "backend": backend_name,
                    "model": model,
                    "protocol": protocol,
                    "vendor_id": vendor_id,
                    "product_id": product_id,
                    "serial_number": serial_number,
                }
                print(f"Found printer: {printer_info}")
                return printer_info

        except Exception as e:
            print(f"Error with backend {backend_name}: {str(e)}")
            continue

    print("No Brother QL printer found")
    return None

def get_printer_label_info():
    printer_info = find_and_parse_printer()
    if not printer_info:
        return None, "No printer found"

    try:
        # First try to get status using brother_ql command line tool
        cmd = f"brother_ql -b pyusb --model {printer_info['model']} -p {printer_info['identifier']} status"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        # If command succeeded and we got output
        if result.returncode == 0 and result.stdout:
            status_output = result.stdout
            print(f"Printer status output: {status_output}")  # Debug print

            # Parse the status output
            media_width_mm = None

            for line in status_output.split('\n'):
                if 'Media size:' in line:
                    try:
                        # Extract just the number before 'x'
                        width_str = line.split(':')[1].split('x')[0].strip()
                        media_width_mm = int(width_str)
                        print(f"Detected media width: {media_width_mm}mm")  # Debug print
                    except ValueError:
                        continue

            if media_width_mm is not None:
                # Map physical width in mm to brother_ql label types
                label_sizes = {
                    12: "12",     # 106 dots printable
                    29: "29",     # 306 dots printable
                    38: "38",     # 413 dots printable
                    50: "50",     # 554 dots printable
                    54: "54",     # 590 dots printable
                    62: "62",     # 696 dots printable
                    102: "102",   # 1164 dots printable
                    103: "103",   # 1200 dots printable
                    104: "104"    # 1200 dots printable
                }

                if media_width_mm in label_sizes:
                    label_type = label_sizes[media_width_mm]
                    return label_type, f"Detected {label_type} ({media_width_mm}mm)"

        # If we couldn't get the width from status, check if it's in printer_info
        if 'model' in printer_info:
            # Default mappings for common models
            model_defaults = {
                'QL-500': "62",
                'QL-550': "62",
                'QL-560': "62",
                'QL-570': "62",
                'QL-580N': "62",
                'QL-650TD': "62",
                'QL-700': "62",
                'QL-710W': "62",
                'QL-720NW': "62",
                'QL-800': "62",
                'QL-810W': "62",
                'QL-820NWB': "62",
                'QL-1050': "102",
                'QL-1060N': "102",
            }
            if printer_info['model'] in model_defaults:
                return model_defaults[printer_info['model']], f"Using default for {printer_info['model']}"

        # If all else fails, return a safe default
        return "62", "Using safe default width"

    except Exception as e:
        print(f"Error getting printer status: {str(e)}")  # Debug print
        return "62", f"Error getting printer status, using default"

def get_label_type():
    """
    Determine label type in order of precedence:
    1. From session state if already detected
    2. From printer's current media (most accurate)
    3. From secrets.toml configuration (fallback)
    4. Default to "62" with warning
    """
    # First check if we already have the label type in session state
    if st.session_state.label_type is not None:
        return st.session_state.label_type, st.session_state.label_status

    # Try to detect from printer's current media
    detected_label, status_message = get_printer_label_info()
    if detected_label:
        print(f"Using detected label type: {detected_label} - {status_message}")  # Debug print
        # Store in session state
        st.session_state.label_type = detected_label
        st.session_state.label_status = status_message
        return detected_label, status_message

    # Try to get from secrets.toml
    if "label_type" in st.secrets:
        configured_type = st.secrets["label_type"]
        status = "Using configured label_type from secrets"
        print(f"Using configured label type from secrets: {configured_type}")  # Debug print
        # Store in session state
        st.session_state.label_type = configured_type
        st.session_state.label_status = status
        return configured_type, status

    # If neither works, return default with warning
    default_type = "62"
    status = "Using default label type 62"
    print("No label type detected or configured, using default 62")  # Debug print
    st.warning("⚠️ No label type detected from printer and none configured in secrets.toml. Using default label type 62")
    # Store in session state
    st.session_state.label_type = default_type
    st.session_state.label_status = status
    return default_type, status

# Get label type and status message
label_type, label_status = get_label_type()

if 'txt2img_url' not in st.secrets:
    st.warning("⚠️ txt2img_url not found in secrets. Using default localhost:8670")
    txt2img_url = "http://localhost:8670"
else:
    txt2img_url = st.secrets['txt2img_url']


def get_label_width(label_type):
    label_definitions = labels.ALL_LABELS
    for label in label_definitions:
        if label.identifier == label_type:
            width = label.dots_printable[0]
            print(f"Label type {label_type} width: {width} dots")  # Debug print
            return width
    raise ValueError(f"Label type {label_type} not found in label definitions")


label_width = get_label_width(label_type)  # Use the width as label_width


# Check if the 'copy' parameter exists
# add to url "?copy=25"
copy = int(st.query_params.get("copy", [1])[0])  # Default to 1 copy if not specified


# Function to list saved images with optional duplicate filtering
def list_saved_images(filter_duplicates=True):
    # Get history limit from secrets with default fallback of 15
    history_limit = st.secrets.get("history_limit", 15)

    # Get all image files from both temp and labels folders
    temp_files = glob.glob(os.path.join("temp", "*.[pj][np][g]*"))
    label_files = glob.glob(os.path.join("labels", "*.[pj][np][g]*"))

    # Combine all image files
    image_files = temp_files + label_files

    # Filter out test labels and get valid images
    valid_images = [
        f for f in image_files
        if "write_something" not in os.path.basename(f).lower()
    ]

    if not filter_duplicates:
        # Simply return all files sorted by modification time
        return sorted(valid_images, key=os.path.getmtime, reverse=True)[:history_limit]

    # Create a dictionary to store the latest version of each unique image size
    unique_images = {}

    for image_path in valid_images:
        try:
            # Get file size in bytes
            file_size = os.path.getsize(image_path)

            # If this size already exists, compare modification times
            if file_size in unique_images:
                existing_time = os.path.getmtime(unique_images[file_size])
                current_time = os.path.getmtime(image_path)
                if current_time > existing_time:
                    unique_images[file_size] = image_path
            else:
                unique_images[file_size] = image_path
        except Exception as e:
            print(f"Error processing {image_path}: {e}")
            continue

    # Sort by modification time (newest first)
    return sorted(unique_images.values(), key=os.path.getmtime, reverse=True)[:history_limit]

def get_fonts():
    return [ "fonts/" + i for i in os.listdir("fonts/") if i.endswith(".ttf")]

def safe_filename(text):
    # Get the current time in epoch format
    epoch_time = int(time.time())
    # Return the filename
    return f"{epoch_time}_{hashlib.sha256(text.encode()).hexdigest()}.png"


# Ensure label directory exists
label_dir = "labels"
os.makedirs(label_dir, exist_ok=True)


def generate_image(prompt, steps):
    payload = {"prompt": prompt, "steps": steps, "width": label_width}

    # Get txt2img_url from secrets with default fallback
    txt2img_url = st.secrets.get("txt2img_url", "http://localhost:7860")

    # Show warning if using default URL
    if txt2img_url == "http://localhost:7860":
        st.warning("Using default Stable Diffusion URL (http://localhost:7860). Configure txt2img_url in .streamlit/secrets.toml for custom endpoint.")

    try:
        response = requests.post(url=f'{txt2img_url}/sdapi/v1/txt2img', json=payload)

        # Check if the request was successful
        response.raise_for_status()

        # Print raw response content for debugging
        print("Raw response content:", response.content)

        r = response.json()

        if r["images"]:
            first_image = r["images"][0]
            base64_data = (
                first_image.split("base64,")[1]
                if "base64," in first_image
                else first_image
            )
            image = Image.open(io.BytesIO(base64.b64decode(base64_data)))

            png_payload = {"image": "data:image/png;base64," + first_image}
            response2 = requests.post(
                url=f"{txt2img_url}/sdapi/v1/png-info", json=png_payload
            )
            response2.raise_for_status()

            # save image
            pnginfo = PngImagePlugin.PngInfo()
            info = response2.json().get("info")
            if info:
                pnginfo.add_text("parameters", str(info))
            current_date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            temp_dir = "temp"
            os.makedirs(temp_dir, exist_ok=True)
            filename = os.path.join(temp_dir, "txt2img_" + current_date + ".png")
            image.save(filename, pnginfo=pnginfo)

            return image
        else:
            print("No images found in the response")
            return None

    except requests.exceptions.RequestException as e:
        print(f"An error occurred while making the request: {e}")
        return None
    except ValueError as e:
        print(f"An error occurred while parsing the JSON response: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return None


def preper_image(image, label_width=label_width):
    # Debug print original image size
    # print(f"Original image size: {image.size}")

    # Handle PNG transparency by adding white background
    if image.mode == "RGBA":
        # Create a white background image
        background = Image.new("RGBA", image.size, "white")
        # Alpha composite the image over the white background
        image = Image.alpha_composite(background, image)
        # Convert to RGB after compositing
        image = image.convert("RGB")

    # Only resize if the image width doesn't match label width
    width, height = image.size
    if width != label_width:
        # Calculate new height maintaining aspect ratio
        new_height = int((label_width / width) * height)
        image = image.resize((label_width, new_height))
        print(f"Resizing image from ({width}, {height}) >> {image.size}")

    # Convert to grayscale if needed
    if image.mode != "L":
        grayscale_image = image.convert("L")
    else:
        grayscale_image = image

    # Apply Floyd-Steinberg dithering
    dithered_image = grayscale_image.convert("1", dither=Image.FLOYDSTEINBERG)

    return grayscale_image, dithered_image


def print_image(image, rotate=0, dither=False):
    """
    Queue a print job and return the job ID.
    The actual printing will be handled by the print queue worker.
    """
    # Ensure the temporary directory exists
    temp_dir = tempfile.gettempdir()
    os.makedirs(temp_dir, exist_ok=True)

    # Save the image to a temporary file
    with tempfile.NamedTemporaryFile(
        suffix=".png", delete=False, dir=temp_dir
    ) as temp_file:
        temp_file_path = temp_file.name
        image.save(temp_file_path, "PNG")
        print(f"Image saved to: {temp_file_path}")  # Print the full path

    # Use find_and_parse_printer to get printer information
    printer_info = find_and_parse_printer()
    if not printer_info:
        st.error(
            "No Brother QL printer found. Please check the connection and try again."
        )
        return False

    # Get the current label type
    label_type, _ = get_label_type()
    print(f"Using label type: {label_type}")  # Debug print

    # Add job to print queue with correct label type
    job_id = print_queue.add_job(
        image,
        rotate=rotate,
        dither=dither,
        printer_info=printer_info,
        temp_file_path=temp_file_path,
        label_type=label_type  # Add label_type to the job parameters
    )

    # Start monitoring job status
    status = print_queue.get_job_status(job_id)

    # Show job status in UI
    status_container = st.empty()
    while status.status in ["pending", "processing"]:
        status_container.info(f"Print job status: {status.status}")
        time.sleep(0.5)
        status = print_queue.get_job_status(job_id)

    if status.status == "completed":
        status_container.success("Print job completed successfully!")
        return True
    else:
        status_container.error(f"Print job failed: {status.error}")
        return False

# Add a new function to show queue status
def show_queue_status():
    """Show the current print queue status in the UI"""
    # Check if queue view is enabled in secrets (default to False)
    if not st.secrets.get("queueview", False):
        return

    status = print_queue.get_queue_status()

    # Only show status if there are jobs in queue or currently processing
    if status["queue_size"] > 0 or status["is_processing"]:
        # Create a small container in the top-right corner
        status_container = st.empty()
        with status_container.container():
            # Make the status display compact
            st.markdown(
                """
                <style>
                div[data-testid="stVerticalBlock"] > div {
                    padding-top: 0;
                    padding-bottom: 0;
                    margin-top: -1em;
                }
                </style>
                """,
                unsafe_allow_html=True
            )

            # Use a single line for each job status
            for job_id, job_info in status["jobs"].items():
                status_color = {
                    "pending": "🟡",
                    "processing": "🔵",
                    "completed": "🟢",
                    "failed": "🔴"
                }.get(job_info["status"], "⚪")

                # Show job ID (first 8 chars) and status
                status_text = f"{status_color} Job {job_id[:8]}: {job_info['status']}"
                if job_info["error"]:
                    status_text += f" ({job_info['error']})"
                st.write(status_text)

# Add queue status display to the main UI
if __name__ == "__main__":
    show_queue_status()

def find_url(string):
    url_pattern = re.compile(
        r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
    )
    urls = re.findall(url_pattern, string)
    return urls


def img_concat_v(im1, im2, image_width=label_width):
    dst = Image.new("RGB", (im1.width, im1.height + image_width))
    dst.paste(im1, (0, 0))
    im2 = im2.resize((image_width, image_width))

    dst.paste(im2, (0, im1.height))
    return dst


def get_cat_breeds():
    cat_api_key = st.secrets.get("cat_api_key", "")
    if not cat_api_key or cat_api_key == "ask me":
        return ["API key required"]

    try:
        response = requests.get(
            "https://api.thecatapi.com/v1/breeds",
            headers={"x-api-key": cat_api_key}
        )
        breeds = response.json()
        return [breed["name"] for breed in breeds]
    except:
        return ["Error fetching breeds"]


def apply_threshold(image, threshold):
    # Ensure the image is in grayscale mode
    if image.mode != 'L':
        image = image.convert('L')

    # Create a LUT with 256 entries
    lut = [255 if i > threshold else 0 for i in range(256)]
    return image.point(lut, mode='1')


def resize_image_to_width(image, target_width_mm, current_dpi=300):
    # Convert mm to inches (since DPI is dots per inch)
    target_width_inch = target_width_mm / 25.4

    # Calculate the target pixel width using the given DPI
    target_width_px = int(target_width_inch * current_dpi)

    # Get current dimensions
    current_width = image.width

    # Calculate scaling factor based on target width
    scale_factor = target_width_px / current_width

    # Calculate new height maintaining aspect ratio
    new_height = int(image.height * scale_factor)

    # Resize the image while maintaining the aspect ratio
    resized_image = image.resize((target_width_px, new_height), Image.LANCZOS)

    # If the resized width is less than label_width pixels, pad with white
    if target_width_px < label_width:
        new_image = Image.new("RGB", (label_width, new_height), (255, 255, 255))
        new_image.paste(resized_image, ((label_width - target_width_px) // 2, 0))
        resized_image = new_image

    print(f"Image resized from {image.width}x{image.height} to {resized_image.width}x{resized_image.height} pixels.")
    print(f"Target width was {target_width_mm}mm ({target_width_px}px)")
    return resized_image


def add_border(image, border_width=1):
    """Add a thin black border around the image"""
    if image.mode == '1':  # Binary image
        # For binary images, create a new binary image with border
        bordered = Image.new('1', (image.width + 2*border_width, image.height + 2*border_width), 0)  # 0 is black
        bordered.paste(image, (border_width, border_width))
        return bordered
    else:
        # For other modes, use ImageOps
        return ImageOps.expand(image, border=border_width, fill='black')


def apply_levels(image, black_point=0, white_point=255):
    """Apply levels adjustment to an image"""
    if image.mode != 'L':
        image = image.convert('L')

    # Create lookup table for the transformation
    lut = []
    for i in range(256):
        # Normalize the input range
        if i <= black_point:
            lut.append(0)
        elif i >= white_point:
            lut.append(255)
        else:
            # Linear mapping between black and white points
            normalized = (i - black_point) / (white_point - black_point)
            lut.append(int(normalized * 255))

    return image.point(lut)


def apply_histogram_equalization(image, black_point=0, white_point=255):
    """Apply histogram equalization with levels adjustment to an image"""
    if image.mode != 'L':
        image = image.convert('L')

    # First apply levels adjustment
    leveled = apply_levels(image, black_point, white_point)

    # Then apply histogram equalization
    return ImageOps.equalize(leveled)


# Streamlit app
if not os.path.exists(".streamlit/secrets.toml"):
    st.error("⚠️ No secrets.toml file found!")
    st.info("""
    Please set up your `.streamlit/secrets.toml` file:
    1. Copy the example file: `cp .streamlit/secrets.toml.example .streamlit/secrets.toml`
    2. Edit the file with your settings

    The app will try to auto-detect your printer's label type, but you can override it in secrets.toml if needed.
    See the example file for all available options and their descriptions.
    """)

st.title(st.secrets.get("title", "STICKER FACTORY"))

st.subheader(":printer: hard copies of images and text")

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs(
    ["Sticker", "Label", "Text2image", "Webcam", "Cat", "Mask Pro", "history", "FAQ"]
)

# Initialize the session state for the prompt and image
if "prompt" not in st.session_state:
    st.session_state.prompt = ""
if "generated_image" not in st.session_state:
    st.session_state.generated_image = None


def submit():
    st.session_state.prompt = st.session_state.widget
    st.session_state.widget = ""
    st.session_state.generated_image = (
        None  # Reset the generated image when a new prompt is entered
    )

# sticker
with tab1:
    st.subheader("Sticker")

    # Allow the user to upload an image
    uploaded_image = st.file_uploader(
        "Choose an image file to print", type=["png", "jpg", "gif", "webp"]
    )

    # Initialize a variable for the image to be processed
    image_to_process = None
    filename = None  # To hold the filename without extension

    # Check if an image has been uploaded
    if uploaded_image is not None:
        # Convert the uploaded file to a PIL Image
        image_to_process = Image.open(uploaded_image).convert("RGB")
        filename = os.path.splitext(uploaded_image.name)[0]

        # Get the original filename without extension
        original_filename_without_extension = (
            os.path.splitext(uploaded_image.name)[0]
            if uploaded_image
            else "selected_image"
        )

        # grayimage = add_white_background_and_convert_to_grayscale(image_to_process)
        grayscale_image, dithered_image = preper_image(image_to_process)

        # Paths to save the original and dithered images in the 'temp' directory with postfix
        original_image_path = os.path.join(
            "temp", original_filename_without_extension + "_original.png"
        )

        # Create checkboxes for rotation and dithering (dither default to True) inline
        col1, col2 = st.columns(2)
        with col1:
            dither_checkbox = st.checkbox(
                "Dither - _use for high detail, true by default_", value=True
            )
        with col2:
            rotate_checkbox = st.checkbox("Rotate - _90 degrees_")

        # Determine the button text based on checkbox states
        button_text = "Print "
        if rotate_checkbox:
            button_text += "Rotated "
        if dither_checkbox:
            button_text += "Dithered "
        button_text += "Image"

        # Create a single button with dynamic text
        if st.button(button_text):
            rotate_value = 90 if rotate_checkbox else 0
            dither_value = dither_checkbox
            print_image(image_to_process, rotate=rotate_value, dither=dither_value)

        # Display image based on checkbox status
        if dither_checkbox:
            st.image(dithered_image, caption="Resized and Dithered Image")
        else:
            st.image(image_to_process, caption="Original Image")

        # Create 'temp' directory if it doesn't exist
        os.makedirs("temp", exist_ok=True)

        # Save original image
        image_to_process.save(original_image_path, "PNG")

# label
with tab2:
    st.subheader(":printer: a label")

    img = ""

    # Function to calculate the actual image height based on the bounding boxes of each line
    def calculate_actual_image_height_with_empty_lines(text, font, line_spacing=10):
        draw = ImageDraw.Draw(
            Image.new("RGB", (1, 1), color="white")
        )  # Dummy image for calculation
        total_height = 0

        # Get font metrics for consistent spacing
        ascent, descent = font.getmetrics()
        font_height = ascent + descent

        for line in text.split("\n"):
            if line.strip():  # Non-empty lines
                # Use textbbox for more accurate measurements
                bbox = draw.textbbox((0, 0), line, font=font)
                text_height = max(
                    bbox[3] - bbox[1], font_height
                )  # Use the larger of bbox height or font height
            else:  # Empty lines
                text_height = font_height

            total_height += text_height + line_spacing

        # Add padding at top and bottom
        padding = 20
        return total_height + (padding * 2)  # Add padding to total height

    # Function to calculate the maximum font size based on the width of the longest line
    def calculate_max_font_size(
        width, text, font_path, start_size=10, end_size=200, step=1
    ):
        try:
            draw = ImageDraw.Draw(
                Image.new("RGB", (1, 1), color="white")
            )  # Dummy image for calculation
            max_font_size = start_size

            for size in range(start_size, end_size, step):
                try:
                    font = ImageFont.truetype(font_path, size)
                except OSError:
                    # If can't load font, use default
                    return 50  # Return reasonable default size
                adjusted_lines = []
                for line in text.split("\n"):
                    adjusted_lines.append(line)

                max_text_width = max(
                    [
                        draw.textbbox((0, 0), line, font=font)[2]
                        for line in adjusted_lines
                        if line.strip()
                    ]
                )

                if max_text_width <= width:
                    max_font_size = size
                else:
                    break

            return max_font_size
        except Exception as e:
            print(f"Error in calculate_max_font_size: {e}")
            return 50  # Return reasonable default size if anything fails

    # Multiline Text Input
    text = st.text_area("Enter your text to print", "write something\n", height=200)
    # Check if the text has been changed by the user
    if text:
        urls = find_url(text)
        if urls:
            st.success("Found URLs: we might automate the QR code TODO")
            for url in urls:
                st.write(url)

        # init some font vars
        fonts = get_fonts()
        font = fonts[0]  # Default to first available font
        alignment = "center"

        # Initialize font selection in session state if not already present
        if "selected_font" not in st.session_state:
            st.session_state.selected_font = fonts[0]

        try:
            # Try to load the font
            test_font = ImageFont.truetype(font, 12)
        except OSError:
            st.error("5x5-Tami.ttf font not found! Please ensure the fonts directory exists and contains 5x5-Tami.ttf")
            st.info("You can download it from the project repository")
            font = None  # Mark font as unavailable

        if font is None:
            st.stop()  # Stop execution if font is not available

        # Calculate initial max size with default font
        try:
            # Get the longest line length
            chars_per_line = max(len(line) for line in text.split('\n'))
            if chars_per_line == 0:
                chars_per_line = 1

            print(f"Debug font sizing:")
            print(f"- Label type: {label_type}")
            print(f"- Label width: {label_width} dots")
            print(f"- Longest line: {chars_per_line} characters")

            # Base sizes for different label types
            if label_type == "62":  # 62mm label
                base_font_size = 60
                base_width = 696
            elif label_type == "102":  # 102mm label
                base_font_size = 107
                base_width = 1164
            else:
                # For other sizes, scale from 62mm base
                base_font_size = 60
                base_width = 696

            # First scale based on width ratio
            width_scale = label_width / base_width
            scaled_base_size = int(base_font_size * width_scale)

            # Then scale based on text length (14 chars is our reference)
            base_text_length = 14  # "write something" length
            text_scale = base_text_length / chars_per_line
            font_size = int(scaled_base_size * text_scale)

            # Ensure minimum size of 20
            font_size = max(font_size, 20)

            print(f"- Base font size: {base_font_size}")
            print(f"- Width scale: {width_scale:.2f}")
            print(f"- Text scale: {text_scale:.2f}")
            print(f"- Final font size: {font_size}")

            max_size = font_size
        except Exception as e:
            max_size = 60  # Default to known good size for 62mm
            font_size = max_size
            print(f"Error calculating font size: {e}")

        # After font size is set, print debug info about the final text layout
        print(f"\nFinal text layout:")
        print(f"- Font: {font}")
        print(f"- Font size: {font_size}")
        print(f"- Label width: {label_width}")
        print(f"- Text: {text}")

        fontstuff = st.checkbox("font settings", value=False)
        col1, col2 = st.columns(2)
        if fontstuff:
            # Font Selection with session state
            with col1:
                font = st.selectbox(
                    "Choose your font",
                    fonts,
                    index=fonts.index(st.session_state.selected_font),
                )
                st.session_state.selected_font = font

            # Alignment
            with col2:
                alignment_options = ["left", "center", "right"]
                alignment = st.selectbox(
                    "Choose text alignment", alignment_options, index=1
                )
            # Recalculate max size if font changed
            try:
                if font == "fonts/5x5-Tami.ttf":
                    chars_per_line = max(len(line) for line in text.split('\n'))
                    if chars_per_line == 0:
                        chars_per_line = 1
                    # Use same calculation as above for consistency
                    if label_type == "62":  # 62mm label
                        font_size = 60
                    elif label_type == "102":  # 102mm label
                        font_size = 107
                    else:
                        # For other sizes, scale based on width ratio from 62mm label
                        base_width = 696  # Width of 62mm label
                        width_scale = label_width / base_width
                        font_size = int(60 * width_scale)  # Scale from base size of 60

                    print(f"Font size recalculated:")
                    print(f"- Label type: {label_type}")
                    print(f"- Label width: {label_width} dots")
                    print(f"- Font size: {font_size}")

                    max_size = font_size
                else:
                    max_size = calculate_max_font_size(label_width, text, font)
            except Exception as e:
                print(f"Error calculating font size for {font}: {e}")
            font_size = st.slider("Font Size", 20, max_size + 50, max_size)
        line_spacing = st.slider("Line Spacing", 0, 200, 10)

        # Font Size
        try:
            fnt = ImageFont.truetype(font, font_size)
        except OSError:
            # If the 5x5 font is not found, try to use default system font
            try:
                fnt = ImageFont.load_default()
                st.warning(f"Font {font} not found, using default font. Please ensure fonts/5x5-Tami.ttf exists.")
            except Exception as e:
                st.error(f"Error loading font: {e}")

        # Calculate the new image height based on the bounding boxes
        new_image_height = calculate_actual_image_height_with_empty_lines(
            text, fnt, line_spacing
        )

        # Create Image with padding
        padding = 20  # Consistent with the padding in calculate_actual_image_height
        img = Image.new("RGB", (label_width, new_image_height), color="white")
        d = ImageDraw.Draw(img)

        # Adjust starting y position to account for padding
        y = padding  # Start from padding instead of 5

        # Draw Text
        for line in text.split("\n"):
            text_width = 0
            ascent, descent = fnt.getmetrics()
            font_height = ascent + descent

            if line.strip():  # For non-empty lines
                bbox = d.textbbox((0, y), line, font=fnt)
                text_width = bbox[2] - bbox[0]
                text_height = max(bbox[3] - bbox[1], font_height)
            else:  # For empty lines
                text_height = font_height

            if alignment == "center":
                x = (label_width - text_width) // 2
            elif alignment == "right":
                x = label_width - text_width
            else:
                x = 0

            d.text((x, y), line, font=fnt, fill=(0, 0, 0))
            y += text_height + line_spacing

        # Save the label image
        if text != "write something":
            filename = safe_filename(text)
            file_path = os.path.join(label_dir, filename)
            img.save(file_path, "PNG")
            st.success(f"Label saved as {filename}")

    # QR code
    qr = qrcode.QRCode(border=0)

    qrurl = st.text_input(
        "add a QRcode to your sticker",
    )
    if qrurl:
        # we have text generate qr
        qr.add_data(qrurl)
        qr.make(fit=True)
        imgqr = qr.make_image(fill_color="black", back_color="white")

        # save to image
        # add random 4 letetrs to file name
        # letters = string.ascii_lowercase
        # random_string = ''.join(random.choice(letters) for i in range(4))
        # qrimgpath = os.path.join('temp', "qr_" + random_string + '.png')
        # imgqr.save(qrimgpath, "PNG")

        if imgqr and img:
            # add qr below the label
            imgqr = img_concat_v(img, imgqr)
            st.image(imgqr, use_container_width=True)
            if st.button("Print sticker+qr", key="print_sticker_qr"):
                print_image(imgqr)
        elif imgqr and not (img):
            if st.button("Print sticker", key="print_qr_only"):
                print_image(imgqr)

    if text and not (qrurl):
        st.image(img, use_container_width=True)
        if st.button("Print sticker", key="print_text_only"):
            print_image(img)  # Needs definition
            st.success("sticker sent to printer")
    st.markdown(
        """
                * label will automaticly resize to fit the longest line, so use linebreaks.
                * on pc `ctrl+enter` will submit, on mobile click outside the `text_area` to process.
                """
    )

# text2img
with tab3:
    st.subheader(":printer: image from text")
    st.write("using tami stable diffusion bot")

    st.text_input("Enter a prompt", key="widget", on_change=submit)
    prompt = st.session_state.prompt

    if prompt and st.session_state.generated_image is None:
        st.write("Generating image from prompt: " + prompt)
        generated_image = generate_image(prompt, 30)
        st.session_state.generated_image = (
            generated_image  # Store the generated image in session state
        )

    if st.session_state.generated_image:
        generated_image = st.session_state.generated_image
        grayscale_image, dithered_image = preper_image(generated_image)

        col1, col2 = st.columns(2)
        with col1:
            st.image(grayscale_image, caption="Original Image")
        with col2:
            st.image(dithered_image, caption="Resized and Dithered Image")

        col3, col4 = st.columns(2)
        with col3:
            if st.button("Print Original Image", key="print_original_t2i"):
                print_image(grayscale_image)
                st.success("Original image sent to printer!")
        with col4:
            if st.button("Print Dithered Image", key="print_dithered_t2i"):
                print_image(grayscale_image, dither=True)
                st.success("Dithered image sent to printer!")

    # Update last prompt
    st.session_state.last_prompt = prompt

# webcam
with tab4:
    st.subheader(":printer: a snapshot")
    on = st.toggle("ask user for camera permission")
    if on:
        picture = st.camera_input("Take a picture")
        if picture is not None:
            picture = Image.open(picture).convert("RGB")
            grayscale_image, dithered_image = preper_image(picture)

            st.image(dithered_image, caption="Resized and Dithered Image")

            # Save webcam image before printing
            filename = safe_filename("webcam")
            file_path = os.path.join(label_dir, filename)
            picture.save(file_path, "PNG")
            st.success(f"Webcam photo saved as {filename}")

            # print options
            colc, cold = st.columns(2)
            with colc:
                if st.button("Print rotated Image", key="print_rotated_webcam"):
                    print_image(grayscale_image, rotate=90, dither=True)
                    st.balloons()
                    st.success("rotated image sent to printer!")
            with cold:
                if st.button("Print Image", key="print_webcam"):
                    print_image(grayscale_image, dither=True)
                    st.success("image sent to printer!")

# cat
with tab5:
    st.subheader(":printer: a cat")
    st.caption("from the fine folks at https://thecatapi.com/")

    # Initialize session state for cat image if not exists
    if 'cat_image' not in st.session_state:
        st.session_state.cat_image = None
        st.session_state.cat_dithered = None

    # Check if Cat API key exists and is valid
    cat_api_key = st.secrets.get("cat_api_key", "")

    if not cat_api_key or cat_api_key == "ask me":
        st.warning("⚠️ Cat API key is not configured")
        st.info("Add your cat_api_key to .streamlit/secrets.toml")
    else:
        if st.button("Fetch cat"):
            try:
                # Get cat image URL
                response = requests.get(
                    "https://api.thecatapi.com/v1/images/search",
                    headers={"x-api-key": cat_api_key}
                )
                response.raise_for_status()
                image_url = response.json()[0]["url"]

                # Download and process image
                img = Image.open(io.BytesIO(requests.get(image_url).content)).convert('RGB')
                grayscale_image, dithered_image = preper_image(img)

                # Store in session state
                st.session_state.cat_image = grayscale_image
                st.session_state.cat_dithered = dithered_image

            except Exception as e:
                st.error(f"Error fetching cat: {str(e)}")

        # Show image and print button if we have a cat
        if st.session_state.cat_dithered is not None:
            st.image(st.session_state.cat_dithered, caption="Cat!")
            if st.button("Print Cat", key="print_cat"):
                print_image(st.session_state.cat_image, dither=True)
                st.success("Cat sent to printer!")

# Add the new mask tab content before the history tab
with tab6:
    st.subheader("Mask Pro")

    uploaded_file = st.file_uploader("Choose an image for mask...", type=["jpg", "jpeg", "png", "gif", "webp", "bmp"], key="mask_uploader")

    if uploaded_file is not None:
        try:
            # Load and convert image to RGB to ensure compatibility
            image = Image.open(uploaded_file)
            if image.mode == "RGBA":
                # Handle transparency
                background = Image.new("RGBA", image.size, "white")
                image = Image.alpha_composite(background, image)
            image = image.convert("RGB")

            col1, col2 = st.columns([1, 1])

            with col1:
                print_choice = st.radio("Choose which image to print/save:", ("Original", "Threshold"))

                st.text("General options:")
                mirror_checkbox = st.checkbox("Mirror Mask", value=False)
                invert_checkbox = st.checkbox("Invert Image", value=False)
                border_checkbox = st.checkbox("Show border in preview", value=True, help="Adds a border in the preview to help visualize boundaries (not printed)")
                equalize_checkbox = st.checkbox("Apply Histogram Equalization", value=False, help="Enhance image contrast")

                # Add target width in mm option
                target_width_mm = st.number_input("Target Width (mm)", min_value=0, value=0)

                # Disable rotation if target width is specified
                rotate_disabled = target_width_mm > 0
                rotate_checkbox = st.checkbox("rotate 90deg", value=False, disabled=rotate_disabled)
                if rotate_disabled and rotate_checkbox:
                    st.info("Rotation disabled when target width is specified")

                # Apply target width resizing if specified
                if target_width_mm > 0:
                    image = resize_image_to_width(image, target_width_mm)

                if mirror_checkbox:
                    image = ImageOps.mirror(image)

                if invert_checkbox:
                    image = ImageOps.invert(image)

                if equalize_checkbox:
                    st.text("Levels Adjustment:")
                    col_levels1, col_levels2 = st.columns(2)
                    with col_levels1:
                        black_point = st.slider("Black Point", 0, 255, 0)
                    with col_levels2:
                        white_point = st.slider("White Point", 0, 255, 255)

                # Apply histogram equalization if selected
                if equalize_checkbox:
                    image = apply_histogram_equalization(image, black_point, white_point)

                # Process image based on choice
                if print_choice == "Original":
                    dither = st.checkbox("Dither - approximate grey tones with dithering", value=True)
                    grayscale_image, dithered_image = preper_image(image)
                    display_image = dithered_image if dither else grayscale_image
                else:  # Threshold
                    threshold_percent = st.slider("Threshold (%)", 0, 100, 50)
                    threshold = int(threshold_percent * 255 / 100)
                    display_image = apply_threshold(image, threshold)

                # Create a copy for display with border if needed
                preview_image = display_image.copy()
                if border_checkbox:
                    preview_image = add_border(preview_image)

            with col2:
                st.image(preview_image, caption="Preview", use_container_width=True)

            print_button_label = f"Print {print_choice} Image"
            if print_choice == "Original" and dither:
                print_button_label += ", Dithering"
            if rotate_checkbox and not rotate_disabled:
                print_button_label += ", Rotated 90°"
            if mirror_checkbox:
                print_button_label += ", Mirrored"
            if invert_checkbox:
                print_button_label += ", Inverted"
            if target_width_mm > 0:
                print_button_label += f", Width: {target_width_mm}mm"

            if st.button(print_button_label):
                rotate = 90 if (rotate_checkbox and not rotate_disabled) else 0
                if print_choice == "Original":
                    print_image(grayscale_image, rotate=rotate, dither=dither)
                else:
                    print_image(display_image, rotate=rotate, dither=False)
                st.success("Print job sent to printer!")

        except Exception as e:
            st.error(f"Error processing image: {str(e)}")
            st.info("Please try another image or format")

# history tab
with tab7:
    st.subheader("Gallery of Labels and Stickers")

    # Only process history if this tab is selected
    if tab7:  # This checks if the tab is active
        # Initialize session state variables if they don't exist
        if 'saved_images_list' not in st.session_state:
            st.session_state.saved_images_list = list_saved_images()
        if 'page_number' not in st.session_state:
            st.session_state.page_number = 0
        if 'search_query' not in st.session_state:
            st.session_state.search_query = ""
        if 'filter_duplicates' not in st.session_state:
            st.session_state.filter_duplicates = True

        # Get pagination settings from secrets with defaults
        items_per_page = st.secrets.get("items_per_page", 5)  # Default to 3x3 grid

        # Search, filter, and refresh controls
        col1, col2, col3 = st.columns([3, 2, 1])
        with col1:
            search_query = st.text_input("Search filenames", value=st.session_state.search_query)
        with col2:
            filter_duplicates = st.checkbox("Filter duplicates", value=st.session_state.filter_duplicates)
            st.session_state.filter_duplicates = filter_duplicates
        with col3:
            if st.button("Refresh Gallery"):
                st.session_state.saved_images_list = list_saved_images(filter_duplicates)
                st.session_state.page_number = 0
                st.rerun()

        # Update image list if filter setting changed
        if filter_duplicates != st.session_state.filter_duplicates:
            st.session_state.saved_images_list = list_saved_images(filter_duplicates)
            st.session_state.page_number = 0
            st.rerun()

        # Filter images based on search query
        filtered_images = [
            img for img in st.session_state.saved_images_list
            if search_query.lower() in os.path.basename(img).lower()
        ]

        # Pagination
        total_pages = max((len(filtered_images) - 1) // items_per_page + 1, 1)

        # Pagination controls
        col1, col2, col3 = st.columns([1, 2, 1])
        with col1:
            if st.button("Previous", disabled=st.session_state.page_number <= 0):
                st.session_state.page_number -= 1
                st.rerun()
        with col2:
            st.write(f"Page {st.session_state.page_number + 1} of {total_pages}")
        with col3:
            if st.button("Next", disabled=st.session_state.page_number >= total_pages - 1):
                st.session_state.page_number += 1
                st.rerun()

        # Calculate start and end indices for current page
        start_idx = st.session_state.page_number * items_per_page
        end_idx = min(start_idx + items_per_page, len(filtered_images))

        # Display current page images
        cols_per_row = 3
        current_page_images = filtered_images[start_idx:end_idx]

        # Show total count of filtered images
        st.caption(f"Showing {len(current_page_images)} of {len(filtered_images)} images")

        # Display images in grid
        for i in range(0, len(current_page_images), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                idx = i + j
                if idx < len(current_page_images):
                    with cols[j]:
                        image_path = current_page_images[idx]
                        image = Image.open(image_path)
                        st.image(image, use_container_width=True)

                        filename = os.path.basename(image_path)
                        modified_time = datetime.fromtimestamp(
                            os.path.getmtime(image_path)
                        ).strftime("%Y-%m-%d %H:%M")
                        st.caption(f"{filename}\n{modified_time}")

                        if st.button(f"Print", key=f"print_{idx}_{st.session_state.page_number}"):
                            print_image(image, dither=True)
                            st.success("Sent to printer!")
    else:
        st.info("Select this tab to view history")

# faq
with tab8:
    st.subheader("FAQ:")
    st.markdown(
        """
        *dithering* is suggested (sometimes inforced) if source is not lineart as grayscale and color look bad at thermal printer

        all uploaded images and generated labels are saved
        uploaded camera snapshot are NOT saved, only printed.

        app [code](https://github.com/5shekel/printit)

        PRINT ALOT is the best!
        """
    )
    st.image(Image.open("assets/station_sm.jpg"), caption="TAMI printshop", use_container_width=True)
