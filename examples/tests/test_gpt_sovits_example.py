import json
import unittest
from pathlib import Path

from astrbot.core.provider.sources.gsv_selfhosted_source import ProviderGSVTTS


class ExampleTests(unittest.TestCase):
    def test_example_matches_builtin_provider_contract(self):
        path = Path(__file__).resolve().parents[1] / 'gpt-sovits-provider.json'
        config = json.loads(path.read_text(encoding='utf-8'))
        provider = ProviderGSVTTS(config, {})
        params = provider.build_synthesis_params('synthetic test')
        self.assertEqual(config['type'], 'gsv_tts_selfhost')
        self.assertEqual(params['text'], 'synthetic test')
        self.assertEqual(params['media_type'], 'wav')
        self.assertIn('ref_audio_path', params)
        self.assertNotIn('gsv_ref_audio_path', params)


if __name__ == '__main__':
    unittest.main()
