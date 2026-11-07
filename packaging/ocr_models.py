"""Build-time provisioning of reproducible OCR weights. Never used by the app."""
import hashlib
from pathlib import Path
import urllib.request

TESS_FAST_REV = '87416418657359cb625c412a48b6e1d6d41c29bd'
TESS_FAST = {
    'eng.traineddata': '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2',
    'rus.traineddata': 'e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225',
}
RAPID_REV = 'v3.10.0'
RAPID_ONNX = {
    'det/ch_PP-OCRv5_det_mobile.onnx': '4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae',
    'rec/cyrillic_PP-OCRv5_rec_mobile.onnx': '90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711',
    'cls/ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx': '54379ae5174d026780215fc748a7f31910dee36818e63d49e17dc598ecc82df7',
}


def fetch_verified(url, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError(f'Bad OCR model checksum: {target.name}')
    target.write_bytes(data)


def prepare_models(vendor):
    for name, expected in TESS_FAST.items():
        url = f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{TESS_FAST_REV}/{name}'
        fetch_verified(url, vendor / 'tesseract' / 'tessdata' / name, expected)
    for model, expected in RAPID_ONNX.items():
        url = f'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/{RAPID_REV}/onnx/PP-OCRv5/{model}'
        fetch_verified(url, vendor / 'rapidocr' / Path(model).name, expected)
    return {**TESS_FAST, **{Path(model).name: expected for model, expected in RAPID_ONNX.items()}}
