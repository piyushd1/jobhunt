import sys
from unittest.mock import MagicMock

# Mock modules that might not be available in the test environment
sys.modules['rich'] = MagicMock()
sys.modules['rich.console'] = MagicMock()
sys.modules['rich.table'] = MagicMock()
sys.modules['yaml'] = MagicMock()
sys.modules['dotenv'] = MagicMock()
sys.modules['playwright'] = MagicMock()
sys.modules['playwright.async_api'] = MagicMock()
sys.modules['structlog'] = MagicMock()
sys.modules['litellm'] = MagicMock()
sys.modules['duckduckgo_search'] = MagicMock()
sys.modules['bs4'] = MagicMock()
sys.modules['google.oauth2.credentials'] = MagicMock()
sys.modules['google_auth_oauthlib.flow'] = MagicMock()
sys.modules['googleapiclient.discovery'] = MagicMock()
sys.modules['google.auth.transport.requests'] = MagicMock()
sys.modules['markdown'] = MagicMock()
sys.modules['chromadb'] = MagicMock()
sys.modules['chromadb.config'] = MagicMock()
sys.modules['chromadb.utils'] = MagicMock()
sys.modules['chromadb.utils.embedding_functions'] = MagicMock()
