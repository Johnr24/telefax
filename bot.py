import logging
import os
import tempfile
import subprocess
from io import BytesIO
from dotenv import load_dotenv
from PIL import Image

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Get configuration from environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CUPS_PRINTER_NAME = os.getenv("CUPS_PRINTER_NAME")
CUPS_SERVER_HOST = os.getenv("CUPS_SERVER_HOST", None) # Optional: Use if CUPS server is remote
ALLOWED_USER_IDS = os.getenv("ALLOWED_USER_IDS", "").split(',')
ALLOWED_USER_IDS = [int(user_id) for user_id in ALLOWED_USER_IDS if user_id.isdigit()] # Convert to list of integers

# --- Constants ---
LABEL_WIDTH_INCHES = 4
LABEL_HEIGHT_INCHES = 6
IMAGE_DPI = 300 # Assume standard print resolution

# Calculate pixel dimensions
LABEL_WIDTH_PX = LABEL_WIDTH_INCHES * IMAGE_DPI
LABEL_HEIGHT_PX = LABEL_HEIGHT_INCHES * IMAGE_DPI

# --- Helper Functions ---

def resize_image(image_bytes):
    """Resizes an image to fit within the label dimensions while maintaining aspect ratio."""
    try:
        img = Image.open(BytesIO(image_bytes))
        img.thumbnail((LABEL_WIDTH_PX, LABEL_HEIGHT_PX), Image.Resampling.LANCZOS)

        # Optional: Create a white background and paste the resized image onto it
        # This ensures the output is always 4x6, even if the aspect ratio doesn't match perfectly.
        # background = Image.new('RGB', (LABEL_WIDTH_PX, LABEL_HEIGHT_PX), (255, 255, 255))
        # paste_x = (LABEL_WIDTH_PX - img.width) // 2
        # paste_y = (LABEL_HEIGHT_PX - img.height) // 2
        # background.paste(img, (paste_x, paste_y))
        # img = background # Use the background image now

        output_buffer = BytesIO()
        # Save as PNG or JPEG, depending on what CUPS handles better (PNG often preferred for graphics)
        img_format = 'PNG' if img.mode == 'RGBA' or 'P' in img.mode else 'JPEG'
        img.save(output_buffer, format=img_format)
        output_buffer.seek(0)
        return output_buffer, img_format.lower()
    except Exception as e:
        logger.error(f"Error resizing image: {e}")
        return None, None

def print_image_cups(image_buffer, printer_name, copies=1, image_format='png'):
    """Sends the image data to the specified CUPS printer."""
    lp_command = ["lp"]

    if CUPS_SERVER_HOST:
        lp_command.extend(["-h", CUPS_SERVER_HOST])

    lp_command.extend(["-d", printer_name])
    lp_command.extend(["-n", str(copies)])
    # Add options for 4x6 media size and scaling. Adjust these based on your printer driver!
    # Common options: 'media=w101h152mm' or 'media=Custom.4x6in'
    # Scaling: 'fit-to-page' or 'scaling=100'
    # You might need to experiment with `lpoptions -p <printer_name> -l` on the CUPS server
    # to find the exact options your printer supports.
    lp_command.extend(["-o", f"media=Custom.{LABEL_WIDTH_INCHES}x{LABEL_HEIGHT_INCHES}in"])
    lp_command.extend(["-o", "fit-to-page"]) # Try to scale the image to fit the media
    # lp_command.extend(["-o", "scaling=100"]) # Alternative: print at 100%

    # Use a temporary file to pass data to lp
    try:
        with tempfile.NamedTemporaryFile(suffix=f'.{image_format}', delete=True) as temp_file:
            temp_file.write(image_buffer.getvalue())
            temp_file.flush() # Ensure data is written to disk

            lp_command.append(temp_file.name) # Add filename to command

            logger.info(f"Executing CUPS command: {' '.join(lp_command)}")
            result = subprocess.run(lp_command, capture_output=True, text=True, check=True)
            logger.info(f"CUPS Output: {result.stdout}")
            logger.info(f"CUPS Error Output: {result.stderr}") # Log stderr as well
            return True, result.stdout
    except subprocess.CalledProcessError as e:
        logger.error(f"CUPS printing failed. Command: '{' '.join(e.cmd)}'")
        logger.error(f"Return code: {e.returncode}")
        logger.error(f"Output: {e.output}")
        logger.error(f"Stderr: {e.stderr}")
        return False, e.stderr
    except Exception as e:
        logger.error(f"An unexpected error occurred during printing: {e}")
        return False, str(e)

def parse_copies(caption):
    """Parses the number of copies from the caption (e.g., 'x3', 'copies=2'). Defaults to 1."""
    if not caption:
        return 1
    caption = caption.lower()
    # Simple check for 'x<number>' or 'copies=<number>'
    import re
    match = re.search(r'(?:x|\bcopies\s*=\s*)(\d+)', caption)
    if match:
        try:
            copies = int(match.group(1))
            return max(1, copies) # Ensure at least 1 copy
        except ValueError:
            pass
    return 1

# --- Telegram Bot Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized access attempt by user {user.id} ({user.username})")
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    await update.message.reply_html(
        rf"Hi {user.mention_html()}! Send me an image to print on the label printer.",
    )

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming photos, resizes them, and sends them to the printer."""
    user = update.effective_user
    if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized image received from user {user.id} ({user.username})")
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    if not update.message.photo:
        await update.message.reply_text("Please send an image file.")
        return

    if not CUPS_PRINTER_NAME:
        logger.error("CUPS_PRINTER_NAME environment variable is not set.")
        await update.message.reply_text("Printer is not configured. Please contact the administrator.")
        return

    # Get the highest resolution photo
    photo_file = await update.message.photo[-1].get_file()
    file_bytes = await photo_file.download_as_bytearray()

    # Parse copies from caption
    caption = update.message.caption
    copies = parse_copies(caption)

    await update.message.reply_text(f"Received image. Resizing and preparing to print {copies} cop{'y' if copies == 1 else 'ies'}...")

    # Resize the image
    resized_image_buffer, image_format = resize_image(file_bytes)

    if not resized_image_buffer:
        await update.message.reply_text("Failed to process the image.")
        return

    # Print the image
    success, message = print_image_cups(resized_image_buffer, CUPS_PRINTER_NAME, copies, image_format)

    if success:
        logger.info(f"Successfully sent image to printer {CUPS_PRINTER_NAME} for user {user.id}")
        await update.message.reply_text(f"Sent to printer! CUPS message: {message}")
    else:
        logger.error(f"Failed to print image for user {user.id}. Error: {message}")
        await update.message.reply_text(f"Failed to send to printer. Error: {message}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    # Optionally, send a message to a specific chat ID (e.g., admin) about the error
    # traceback_str = ''.join(traceback.format_exception(None, context.error, context.error.__traceback__))
    # await context.bot.send_message(chat_id=DEVELOPER_CHAT_ID, text=f"An error occurred: {context.error}\n{traceback_str[:4000]}")


def main() -> None:
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable is not set. Exiting.")
        return
    if not CUPS_PRINTER_NAME:
        logger.warning("CUPS_PRINTER_NAME environment variable is not set. Printing will fail.")
        # Allow starting, but printing won't work until configured.

    if ALLOWED_USER_IDS:
        logger.info(f"Bot access restricted to user IDs: {ALLOWED_USER_IDS}")
    else:
        logger.warning("ALLOWED_USER_IDS is not set. The bot is open to everyone!")


    # Create the Application and pass it your bot's token.
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))

    # on non command i.e message - handle the image message
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_image))

    # Add error handler
    application.add_error_handler(error_handler)

    # Run the bot until the user presses Ctrl-C
    logger.info("Starting bot polling...")
    application.run_polling()

if __name__ == "__main__":
    main()
