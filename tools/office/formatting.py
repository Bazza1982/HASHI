"""Deterministic formatting and native shape helpers for the Office worker."""
from __future__ import annotations

PRESETS = {
    "professional": ("Calibri", "17365D", "FFFFFF", 11),
    "clean": ("Aptos", "286F6C", "FFFFFF", 11),
    "academic": ("Cambria", "333333", "FFFFFF", 12),
}


def word_preset(document, name):
    from docx.shared import Pt
    font, accent, background, size = PRESETS[name]
    document.styles["Normal"].font.name = font
    document.styles["Normal"].font.size = Pt(size)
    document.styles["Normal"].paragraph_format.line_spacing = 1.5 if name == "academic" else 1.15
    for level in (1, 2, 3):
        document.styles["Heading " + str(level)].font.name = font


def slide_preset(deck, name, start=0):
    from pptx.dml.color import RGBColor
    from pptx.util import Pt
    font, accent, background, size = PRESETS[name]
    for index in range(start, len(deck.slides)):
        slide = deck.slides[index]
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string(background)
        for shape in slide.shapes:
            if shape.has_text_frame:
                title = slide.shapes.title is not None and shape.shape_id == slide.shapes.title.shape_id
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.name = font
                        run.font.size = Pt(30 if title else 20)
                        run.font.color.rgb = RGBColor.from_string(accent if title else "202020")
            if shape.has_table:
                for row_index, row in enumerate(shape.table.rows):
                    for cell in row.cells:
                        if row_index == 0:
                            cell.fill.solid()
                            cell.fill.fore_color.rgb = RGBColor.from_string(accent)
                        for paragraph in cell.text_frame.paragraphs:
                            for run in paragraph.runs:
                                run.font.name = font
                                run.font.size = Pt(14)
                                if row_index == 0:
                                    run.font.color.rgb = RGBColor(255,255,255)


def slide_and_shape(deck, target, *, shape_required=False):
    import re
    match = re.fullmatch(r"slide:(\d+)(?:/shape:(\d+))?", target or "")
    if not match:
        raise ValueError("Use slide:1 or slide:1/shape:3 (1-based indices).")
    index = int(match[1])-1
    if not 0 <= index < len(deck.slides):
        raise ValueError("Slide not found.")
    slide = deck.slides[index]
    shape = None
    if match[2]:
        shape_index = int(match[2])-1
        if not 0 <= shape_index < len(slide.shapes):
            raise ValueError("Shape not found.")
        shape = slide.shapes[shape_index]
    if shape_required and shape is None:
        raise ValueError("A shape index is required.")
    return slide, shape


def add_native_chart(slide, spec, region):
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches
    data = CategoryChartData()
    data.categories = spec["categories"]
    for series in spec["series"]:
        data.add_series(str(series["name"]), series["values"])
    kind = {"bar":XL_CHART_TYPE.COLUMN_CLUSTERED,"line":XL_CHART_TYPE.LINE,"pie":XL_CHART_TYPE.PIE}[spec.get("type","bar")]
    slide.shapes.add_chart(kind, *(Inches(v) for v in region), data)


def set_table(table, rows):
    if len(rows) > len(table.rows) or any(len(row)>len(table.columns) for row in rows):
        raise ValueError("Replacement data exceeds existing table dimensions.")
    for row_index,row in enumerate(rows):
        for column,value in enumerate(row):
            table.cell(row_index,column).text = "" if value is None else str(value)


def regions(deck, names):
    width, height = deck.slide_width/914400, deck.slide_height/914400
    names = list(names)
    count = len(names)
    if not count:
        return {}
    columns = 1 if count == 1 else 2
    rows = (count+columns-1)//columns
    gap = .25
    cell_width = (width-1.2-gap*(columns-1))/columns
    cell_height = (height-2.0-gap*(rows-1))/rows
    return {name:(.6+(i%columns)*(cell_width+gap),
                  1.6+(i//columns)*(cell_height+gap),cell_width,cell_height)
            for i,name in enumerate(names)}


def picture_fit(slide, path, region):
    from PIL import Image
    from pptx.util import Inches
    x,y,width,height = region
    with Image.open(path) as picture:
        if picture.width*picture.height > 20_000_000:
            raise ValueError("Image exceeds 20 megapixels.")
        ratio = picture.width/picture.height
        picture.verify()
    actual_width = min(width,height*ratio)
    actual_height = actual_width/ratio
    return slide.shapes.add_picture(str(path),Inches(x+(width-actual_width)/2),
              Inches(y+(height-actual_height)/2),width=Inches(actual_width),height=Inches(actual_height))


def free_region(deck, slide, explicit=None):
    if explicit is not None:
        if not isinstance(explicit,list) or len(explicit)!=4 or not all(isinstance(v,(int,float)) for v in explicit):
            raise ValueError("position must be [left,top,width,height] in inches.")
        x,y,w,h=explicit
        if x<0 or y<0 or w<=0 or h<=0 or x+w>deck.slide_width/914400 or y+h>deck.slide_height/914400:
            raise ValueError("position is outside slide bounds.")
        return tuple(explicit)
    for region in regions(deck,range(4)).values():
        x,y,w,h=(int(v*914400) for v in region)
        occupied = any(not(shape.has_text_frame and not shape.text.strip()) and
                       shape.left < x+w and shape.left+shape.width > x and
                       shape.top < y+h and shape.top+shape.height > y for shape in slide.shapes)
        if not occupied:
            return region
    raise ValueError("LAYOUT_FULL: no free body region. Add a slide or supply an explicit position.")
