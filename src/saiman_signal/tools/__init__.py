from saiman_signal.tools.get_page_contents import DEFINITION as GET_PAGE_CONTENTS_DEF
from saiman_signal.tools.get_page_contents import execute as get_page_contents
from saiman_signal.tools.reddit_read import DEFINITION as REDDIT_READ_DEF
from saiman_signal.tools.reddit_read import execute as reddit_read
from saiman_signal.tools.reddit_search import DEFINITION as REDDIT_SEARCH_DEF
from saiman_signal.tools.reddit_search import execute as reddit_search
from saiman_signal.tools.web_search import DEFINITION as WEB_SEARCH_DEF
from saiman_signal.tools.web_search import execute as web_search

TOOL_DEFINITIONS = [WEB_SEARCH_DEF, GET_PAGE_CONTENTS_DEF, REDDIT_SEARCH_DEF, REDDIT_READ_DEF]

TOOLS = {
    "web_search": web_search,
    "get_page_contents": get_page_contents,
    "reddit_search": reddit_search,
    "reddit_read": reddit_read,
}
