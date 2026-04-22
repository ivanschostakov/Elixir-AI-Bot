from openai import OpenAI

client = OpenAI(api_key="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")

files = client.files.list()
for file in files:
    result = client.files.delete(file.id)
    print(result)
