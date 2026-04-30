from dotenv import load_dotenv      
import anthropic, os
load_dotenv()                                   
c = anthropic.Anthropic()
r = c.messages.create(model='claude-haiku-4-5-20251001',   
max_tokens=10, messages=[{'role':'user','content':'hi'}])
print(r)