import copy
import unittest
from brain.drawing import validate_drawing


class DrawingTests(unittest.TestCase):
    def setUp(self):
        self.drawing={'version':1,'width':1000,'height':750,'title':'庭院','background':'#fffaf0',
                      'strokes':[{'phase':'轮廓','color':'#123456','width':3,'duration':500,
                                  'points':[[100,100,.5],[200,150,1]]}]}

    def test_valid_strokes_and_no_executable_fields(self):
        self.drawing['script']='alert(1)'
        self.assertNotIn('script',validate_drawing(self.drawing))
        self.assertEqual(validate_drawing(self.drawing)['strokes'][0]['points'][0][2],.5)

    def test_bad_coordinates_pressure_and_nan_rejected(self):
        for point in ([1001,0,1],[0,-1,1],[0,0,2],[float('nan'),0,1],[True,0,1]):
            with self.subTest(point=point):
                d=copy.deepcopy(self.drawing);d['strokes'][0]['points'][0]=point
                with self.assertRaises(ValueError):validate_drawing(d)

    def test_resource_limits_and_urls_rejected(self):
        for field,value in [('duration',10001),('width',100),('color','url(http://evil)'),('points',[[0,0,1]])]:
            with self.subTest(field=field):
                d=copy.deepcopy(self.drawing);d['strokes'][0][field]=value
                with self.assertRaises(ValueError):validate_drawing(d)
        self.drawing['strokes']*=501
        with self.assertRaises(ValueError):validate_drawing(self.drawing)
