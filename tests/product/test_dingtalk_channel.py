from __future__ import annotations
import io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dynamic_firm.cli import EXIT_OK,main
from dynamic_firm.product.dingtalk_channel import DingTalkChannelConfig,deliver_dingtalk_message,dingtalk_channel_config_from_settings,remove_dingtalk_channel_settings,write_dingtalk_channel_settings
class _Response:
 def __enter__(self):return self
 def __exit__(self,*_):return None
 def read(self,_):return b'{"errcode":0}'
class DingTalkChannelTests(unittest.TestCase):
 def test_settings_and_static_webhook_delivery(self):
  old=os.environ.get("DINGTALK_TEST_WEBHOOK");os.environ["DINGTALK_TEST_WEBHOOK"]="https://oapi.dingtalk.com/robot/send?access_token=secret"
  try:
   with tempfile.TemporaryDirectory() as directory:
    path=Path(directory)/"config.toml";path.write_text('[provider]\nmodel = "fixture"\n',encoding="utf-8");config=DingTalkChannelConfig("DINGTALK_TEST_WEBHOOK")
    write_dingtalk_channel_settings(path,config);self.assertEqual(dingtalk_channel_config_from_settings(__import__("tomllib").loads(path.read_text(encoding="utf-8"))),config)
    with patch("dynamic_firm.product.dingtalk_channel.urlopen",return_value=_Response()) as opener: result=deliver_dingtalk_message(config,title="Test",message="hello")
    request=opener.call_args.args[0];self.assertEqual(request.full_url,os.environ["DINGTALK_TEST_WEBHOOK"]);self.assertEqual(json.loads(request.data.decode()),{"msgtype":"markdown","markdown":{"title":"Test","text":"hello"}});self.assertTrue(result.delivered);self.assertNotIn("access_token",str(result.to_dict()));self.assertTrue(remove_dingtalk_channel_settings(path))
  finally:
   if old is None:os.environ.pop("DINGTALK_TEST_WEBHOOK",None)
   else:os.environ["DINGTALK_TEST_WEBHOOK"]=old
 def test_cli_confirm_and_capability(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/"config.toml";out=io.StringIO();code=main(["--config",str(path),"channel","dingtalk-configure","--json"],stdout=out,stderr=io.StringIO());caps=io.StringIO();status=main(["--config",str(path),"capabilities","status","--json"],stdout=caps,stderr=io.StringIO());denied=main(["--config",str(path),"channel","dingtalk-test","--message","hello"],stdout=io.StringIO(),stderr=io.StringIO())
  self.assertEqual(code,EXIT_OK);self.assertTrue(json.loads(out.getvalue())["configuration_changed"]);self.assertEqual(status,EXIT_OK);self.assertTrue(json.loads(caps.getvalue())["dingtalk_channel"]["enabled"]);self.assertNotEqual(denied,EXIT_OK)
if __name__=="__main__":unittest.main()
