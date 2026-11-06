"""Diagnostic raster preview of the same pen-path data used by the canvas."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from brain.drawing import load_drawing
from PIL import Image,ImageDraw

path=Path(sys.argv[1])
data=load_drawing(path)
image=Image.new('RGB',(data['width']*2,data['height']*2),data['background'])
draw=ImageDraw.Draw(image)
for stroke in data['strokes']:
    for p,q in zip(stroke['points'],stroke['points'][1:]):
        width=max(1,round(stroke['width']*(p[2]+q[2])))
        draw.line((p[0]*2,p[1]*2,q[0]*2,q[1]*2),fill=stroke['color'],width=width)
        r=width/2
        for point in (p,q):
            x,y=point[0]*2,point[1]*2
            draw.ellipse((x-r,y-r,x+r,y+r),fill=stroke['color'])
image.resize((data['width'],data['height']),Image.Resampling.LANCZOS).save(path.with_name('preview.png'))
print(path.with_name('preview.png'))
