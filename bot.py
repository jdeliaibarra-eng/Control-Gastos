"""Bot de Telegram para controlar gastos.

Envías una captura de un voucher (o texto de una notificación) y el bot:
1. Usa Gemini para extraer monto, fecha, comercio y categoría.
2. Lo guarda en una hoja de Google Sheets.
3. Te confirma por Telegram.

Comandos: /resumen (mes actual por categoría), /ultimos (últimos 5 gastos)
"""
import asyncio
import base64
import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from google import genai
from pydantic import BaseModel, Field
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # evita que el token salga en los logs
log = logging.getLogger("gastos-bot")

# --- Configuración (variables de entorno, nunca en el código) ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
SCRIPT_URL = os.environ["SCRIPT_URL"]  # enlace de la aplicación web de Apps Script
SCRIPT_KEY = os.environ["SCRIPT_KEY"]  # clave secreta que tú elegiste
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Lima"))

CATEGORIAS = [
    "Comida",
    "Transporte",
    "Supermercado",
    "Salud",
    "Entretenimiento",
    "Servicios",
    "Compras",
    "Educación",
    "Transferencias",
    "Otros",
]
HEADERS = ["Fecha", "Monto", "Moneda", "Comercio", "Categoría", "Registrado"]

gemini = genai.Client()  # lee GEMINI_API_KEY


class Gasto(BaseModel):
    es_gasto: bool = Field(
        description="True si la imagen o el texto es un comprobante o aviso de un pago/gasto."
    )
    monto: Optional[float] = Field(description="Monto total pagado, solo el número.")
    moneda: str = Field(description="PEN, USD, etc. Usa PEN si ves 'S/'.")
    fecha: Optional[str] = Field(description="Fecha del pago en formato YYYY-MM-DD.")
    comercio: Optional[str] = Field(description="Comercio o persona que recibió el pago.")
    categoria: str = Field(description="Una de: " + ", ".join(CATEGORIAS))


# --- Google Sheets (vía Apps Script) ---
def guardar(g: Gasto, fecha: str):
    r = httpx.post(
        SCRIPT_URL,
        json={
            "clave": SCRIPT_KEY,
            "fecha": fecha,
            "monto": g.monto,
            "moneda": g.moneda,
            "comercio": g.comercio or "",
            "categoria": g.categoria if g.categoria in CATEGORIAS else "Otros",
            "registrado": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        },
        follow_redirects=True,
        timeout=30,
    )
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError(f"Apps Script respondió: {r.text[:200]}")


# --- Extracción con Gemini ---
def extraer(imagen: Optional[bytes], mime: str, texto: Optional[str]) -> Gasto:
    hoy = datetime.now(TZ).strftime("%Y-%m-%d")
    instruccion = (
        f"Hoy es {hoy} (zona horaria Lima, Perú). Extrae los datos del gasto. "
        "Si la fecha no aparece o no tiene año, usa el año actual o la fecha de hoy. "
        "Si no es un comprobante de pago, pon es_gasto en false. "
        f"Categorías válidas: {', '.join(CATEGORIAS)}."
    )
    entrada = [{"type": "text", "text": instruccion}]
    if texto:
        entrada.append({"type": "text", "text": f"Texto recibido:\n{texto}"})
    if imagen:
        entrada.append(
            {"type": "image", "data": base64.b64encode(imagen).decode(), "mime_type": mime}
        )
    resp = gemini.interactions.create(
        model=GEMINI_MODEL,
        input=entrada,
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": Gasto.model_json_schema(),
        },
    )
    return Gasto.model_validate_json(resp.output_text)


# --- Handlers de Telegram ---
def es_dueña(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == OWNER_ID


async def procesar(update: Update, imagen: Optional[bytes], mime: str, texto: Optional[str]):
    aviso = await update.message.reply_text("Leyendo…")
    try:
        g = await asyncio.to_thread(extraer, imagen, mime, texto)
    except Exception:
        log.exception("Error con Gemini")
        await aviso.edit_text("No pude leer eso. Intenta de nuevo.")
        return
    if not g.es_gasto or g.monto is None:
        await aviso.edit_text("No encontré un gasto en eso. ¿Es un voucher?")
        return
    fecha = g.fecha or datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        await asyncio.to_thread(guardar, g, fecha)
    except Exception:
        log.exception("Error con Google Sheets")
        await aviso.edit_text("Lo leí pero no pude guardarlo en la hoja.")
        return
    cat = g.categoria if g.categoria in CATEGORIAS else "Otros"
    await aviso.edit_text(
        f"✅ {g.moneda} {g.monto:.2f} · {cat}\n{g.comercio or 'Sin comercio'} · {fecha}"
    )


async def on_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueña(update):
        return
    archivo = await update.message.photo[-1].get_file()
    datos = bytes(await archivo.download_as_bytearray())
    await procesar(update, datos, "image/jpeg", update.message.caption)


async def on_documento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueña(update):
        return
    doc = update.message.document
    if not (doc.mime_type or "").startswith("image/"):
        await update.message.reply_text("Mándame una imagen del voucher.")
        return
    archivo = await doc.get_file()
    datos = bytes(await archivo.download_as_bytearray())
    await procesar(update, datos, doc.mime_type, update.message.caption)


async def on_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueña(update):
        return
    await procesar(update, None, "", update.message.text)


def _filas():
    r = httpx.get(SCRIPT_URL, params={"clave": SCRIPT_KEY}, follow_redirects=True, timeout=30)
    r.raise_for_status()
    valores = r.json()["filas"]
    if len(valores) < 2:
        return []
    cab = valores[0]
    return [dict(zip(cab, fila)) for fila in valores[1:]]


async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueña(update):
        return
    mes = datetime.now(TZ).strftime("%Y-%m")
    filas = await asyncio.to_thread(_filas)
    totales, total = defaultdict(float), 0.0
    for f in filas:
        if str(f.get("Fecha", "")).startswith(mes):
            try:
                m = float(f.get("Monto") or 0)
            except ValueError:
                continue
            totales[f.get("Categoría") or "Otros"] += m
            total += m
    if not totales:
        await update.message.reply_text("Aún no hay gastos este mes.")
        return
    lineas = [f"{c}: {m:.2f}" for c, m in sorted(totales.items(), key=lambda x: -x[1])]
    await update.message.reply_text(f"Resumen {mes}\n" + "\n".join(lineas) + f"\n\nTotal: {total:.2f}")


async def ultimos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_dueña(update):
        return
    filas = (await asyncio.to_thread(_filas))[-5:]
    if not filas:
        await update.message.reply_text("Aún no hay gastos.")
        return
    await update.message.reply_text(
        "\n".join(
            f"{f['Fecha']} · {f['Moneda']} {f['Monto']} · {f['Categoría']} · {f['Comercio']}"
            for f in filas
        )
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Error al procesar un mensaje", exc_info=context.error)
    if isinstance(update, Update) and update.message and es_dueña(update):
        try:
            await update.message.reply_text("Algo falló. Vuelve a enviarlo en un momento.")
        except Exception:
            pass


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("resumen", resumen))
    app.add_handler(CommandHandler("ultimos", ultimos))
    app.add_handler(MessageHandler(filters.PHOTO, on_foto))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_documento))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_texto))

    base_url = os.environ.get("RENDER_EXTERNAL_URL")  # Render lo define solo
    if base_url:
        # Modo webhook (hosting): Telegram avisa al bot cuando llega un mensaje.
        secreto = os.environ["WEBHOOK_SECRET"]
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
            url_path=secreto,
            webhook_url=f"{base_url.rstrip('/')}/{secreto}",
            secret_token=secreto,
        )
    else:
        # Modo local (tu computadora): el bot pregunta a Telegram por mensajes nuevos.
        app.run_polling()


if __name__ == "__main__":
    main()
