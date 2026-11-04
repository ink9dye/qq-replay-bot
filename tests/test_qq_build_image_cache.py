import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bot import deck_image


class BuildImageCacheTests(unittest.TestCase):
    def test_same_image_reused_across_calls_and_card_changes_invalidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'art';path.write_bytes(b'art')
            result={'person':{'player':'p','deck_type':'name'},'cards':[{'code':123,'copies':1,'partition':'main'}]}
            def draw(*args,**kwargs):kwargs['stats']['missing']=0;return b'\xff\xd8jpeg'
            with patch.object(deck_image,'render',side_effect=draw) as render:
                for _ in range(2):self.assertEqual(deck_image.render_cached(result,lambda code:path,Path(tmp)/'cache'),b'\xff\xd8jpeg')
                self.assertEqual(render.call_count,1)
                path.write_bytes(b'new image')
                deck_image.render_cached(result,lambda code:path,Path(tmp)/'cache')
                self.assertEqual(render.call_count,2)
                result['person']['deck_type']='renamed'
                deck_image.render_cached(result,lambda code:path,Path(tmp)/'cache')
                self.assertEqual(render.call_count,3)

    def test_missing_images_are_never_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            def draw(*args,**kwargs):kwargs['stats']['missing']=1;return b'\xff\xd8jpeg'
            with patch.object(deck_image,'render',side_effect=draw) as render:
                for _ in range(2):deck_image.render_cached({'cards':[]},lambda code:None,tmp)
                self.assertEqual(render.call_count,2)
            self.assertEqual(list(Path(tmp).glob('*.jpg')),[])
