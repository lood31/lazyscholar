"""Small, self-contained PDF fixtures: real text, vectors and a raster image."""
from __future__ import annotations

import zlib


def pdf_bytes(content: str) -> bytes:
    stream = content.encode("ascii")
    pixels = zlib.compress(bytes([255, 0, 0, 0, 0, 255, 0, 255, 0, 255, 255, 0]))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R /F2 6 0 R >> /XObject << /Im1 7 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Symbol >>",
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length " + str(len(pixels)).encode() + b" >>\nstream\n" + pixels + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def paper_fixture() -> bytes:
    lines = ["BT /F1 18 Tf 36 756 Td (A locally reflowed paper) Tj ET"]
    for x, name in ((36, "Left"), (330, "Right")):
        for index in range(10):
            lines.append(f"BT /F1 10 Tf {x} {700-index*14} Td ({name} paragraph line {index} with readable spaces.) Tj ET")
    lines.extend([
        "q 120 0 0 60 360 100 cm /Im1 Do Q",
        "36 350 m 280 350 l S 36 380 m 280 380 l S 36 410 m 280 410 l S",
        "36 350 m 36 410 l S 160 350 m 160 410 l S 280 350 m 280 410 l S",
        "BT /F1 10 Tf 45 390 Td (Metric) Tj 130 0 Td (Score) Tj ET",
        "BT /F1 10 Tf 45 365 Td (Accuracy) Tj 130 0 Td (0.95) Tj ET",
        "BT /F2 13 Tf 70 240 Td (a+b=c) Tj ET",
        "50 150 190 60 re S 50 150 m 240 210 l S",
    ])
    return pdf_bytes("\n".join(lines))
