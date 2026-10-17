# Diagramas del README

Los diagramas siguen el estilo de Cati con el tema **Moodpeak de Omarchy**: monoespaciada, paneles oscuros, bordes rectos y selección turquesa. La referencia es el CSS de la aplicación y la paleta que carga con `theme: auto`.

| Figura | Imagen | Prompt |
| --- | --- | --- |
| 1 · Consulta, filtros y resultados | [busqueda-natural.png](busqueda-natural.png) | [busqueda-natural.txt](prompts/busqueda-natural.txt) |
| 2 · Preparación del índice y búsqueda | [arquitectura-local.png](arquitectura-local.png) | [arquitectura-local.txt](prompts/arquitectura-local.txt) |

Las capturas WebP usan archivos de demostración. Los diagramas PNG se editaron con la herramienta integrada `image_gen` de Codex. Un [segundo prompt de acabado](prompts/acabado-cati-omarchy.txt) ajusta la jerarquía de los títulos y los rellenos de ambas figuras.

## Paleta de referencia

Valores leídos de `~/.config/omarchy/current/theme/colors.toml` y comprobados con `theme_colors('auto')` el 17 de octubre de 2026. Coinciden con la [paleta de Moodpeak](https://github.com/HANCORE-linux/omarchy-moodpeak-theme/blob/moodpeak-v2/colors.toml).

| Uso | Token de Cati | Color |
| --- | --- | --- |
| Fondo principal | `background` | `#181c22` |
| Cabecera, pie y paneles interiores | `darker_background` | `#101318` |
| Superficie elevada | `lighter_background` | `#23272d` |
| Fondo de selección | `selection` | `#2c3444` |
| Texto principal | `foreground` | `#e0e6ed` |
| Metadatos | `muted` | `#7b8599` |
| Bordes y separadores | `color8` | `#3d4455` |
| Flechas y selección activa | `accent` | `#4ecdc4` |
| Imágenes y anotaciones semánticas | `magenta` | `#b388eb` |

Omarchy define los colores por tema mediante `colors.toml`; consulta su [documentación de temas](https://github.com/omacom/omarchy/blob/quattro/docs/theming.md). Para estos diagramas se fija Moodpeak. Otro tema activo puede cambiar el aspecto de Cati.

## Tipografía y formas

La fuente de Cati es **JetBrainsMono Nerd Font**, con JetBrains Mono y `monospace` como alternativas. Los diagramas usan monoespaciada en títulos, consultas, etiquetas y pies. Los pesos son regular y negrita.

El CSS de la aplicación usa cuerpo de 13 px y consulta de 21 px. Los prompts conservan esa proporción: cuerpo de 32 px y título de 52 px sobre un lienzo lógico de 1920 px de ancho. Los títulos de nodo usan 34 px; las etiquetas auxiliares, 26–28 px. El README muestra las figuras a un máximo de 960 px y ofrece un enlace para ampliarlas.

Los paneles tienen esquinas rectas y bordes finos. La selección combina un fondo más claro con una franja turquesa a la izquierda, como las filas seleccionadas de Cati. Los prompts fijan bordes de 2 px y franjas de 6 px a la escala de exportación. Las flechas siguen recorridos horizontales o con giros de 90°.

Los colores y las formas proceden de [theme_colors y css](../cati/gui.py) y de [explorer_css](../cati/explorer.py). La captura [buscar-fotos.webp](buscar-fotos.webp) sirve de referencia para la fuente, la selección y la separación entre paneles. Los valores hexadecimales del prompt tienen prioridad sobre los colores de esa captura.

## Uso de los prompts

Cada prompt de figura incluye la paleta, las fuentes, las medidas, los textos literales y el recorrido de las flechas. Para editar una figura con ImageGen, adjunta las referencias en este orden:

1. El diagrama que vas a editar, como referencia de contenido y conexiones.
2. La captura de Cati, como referencia de estilo.

Aplica después el prompt de acabado con el mismo orden de referencias. Comprueba los acentos, que las etiquetas quepan dentro de los paneles y que las flechas lleguen al nodo correcto. En la figura 2, el conector del índice termina en **Filtros y coincidencias**.

Los PNG guardan la salida de ImageGen; las medidas de los prompts describen el lienzo lógico de composición.
