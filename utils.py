import base64


def escape_markdown(text):
    """Escapa caracteres especiales para evitar errores de Telegram BadRequest"""
    if not text:
        return ""
    parse_chars = ['_', '*', '`', '[']
    for char in parse_chars:
        text = text.replace(char, f"\\{char}")
    return text


def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')
