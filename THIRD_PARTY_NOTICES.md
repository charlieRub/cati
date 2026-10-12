# Avisos de terceros

Cati se distribuye con licencia MIT (ver `LICENSE`). Usa o adapta los siguientes componentes, cuyas licencias exigen conservar estos avisos.

## Código adaptado

**DigUp** — MIT, Copyright (c) 2026 Abdur Rahim.
Cati adapta de este proyecto los prefijos de las consultas al modelo de embeddings, la calibración de puntuaciones por modalidad, los presupuestos de tokens para imágenes y el parche de `patches/llama-no-logits.patch`.

```
MIT License

Copyright (c) 2026 Abdur Rahim

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Componentes que el instalador descarga

No se incluyen en el repositorio; `install.sh` los descarga y verifica en tu equipo.

| Componente | Uso | Licencia |
| --- | --- | --- |
| [llama.cpp / ggml](https://github.com/ggml-org/llama.cpp) (tag `b11461`) | Motor local de embeddings; se compila con el parche de `patches/` | MIT, Copyright (c) 2023-2026 The ggml authors |
| [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2), GGUF Q8 de `ggml-org` | Modelo de texto, imagen y audio | Según su ficha de modelo |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (`v1.9.5`) y modelos [Whisper](https://huggingface.co/ggerganov/whisper.cpp) | Búsqueda por voz | MIT |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) y modelos [PP-OCRv6](https://github.com/PaddlePaddle/PaddleOCR) | Texto en imágenes, capturas y escaneos | Apache 2.0 |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | Ejecución de los modelos de OCR | MIT |
| [Tesseract](https://github.com/tesseract-ocr/tesseract) (del sistema) | OCR de respaldo | Apache 2.0 |
| [Symbols Nerd Font](https://github.com/ryanoasis/nerd-fonts) | Iconos de la interfaz, solo si no tienes ninguna Nerd Font | MIT / SIL OFL 1.1 |

## Imágenes de las pruebas

Atribuciones en `tests/fixtures/ATTRIBUTION.md` (CC BY-SA 2.0 y CC BY 2.0). Esas imágenes conservan su licencia y no quedan cubiertas por la MIT de Cati.
