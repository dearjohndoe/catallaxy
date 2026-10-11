import json, sys
req = json.load(sys.stdin)
if req.get("mode") == "describe":
    print(json.dumps({"args_schema": {"text": {"type": "string", "required": True}},
                      "result_schema": {"type": "string"}}))
    sys.exit(0)
text = (req.get("body") or {}).get("text", "")
if text == "oos":
    print(json.dumps({"error": "out_of_stock", "reason": "devnet test"}))
else:
    print(json.dumps({"result": {"type": "string", "data": "echo:" + text}}))
