"""A lexical analyzer class for simple shell-like syntaxes."""

# Module and documentation by Eric S. Raymond, 21 Dec 1998
# Input stacking and error message cleanup added by ESR, March 2000
# push_source() and pop_source() made explicit by ESR, January 2001.
# Posix compliance, split(), string arguments, and
# iterator interface by Gustavo Niemeyer, April 2003.
# changes to tokenize more like Posix shells by Vinay Sajip, July 2016.

import os
import re
import sys
from collections import deque

from io import StringIO

__all__ = ["shlex", "split", "quote", "join"]

class shlex:
    "A lexical analyzer class for simple shell-like syntaxes."
    def __init__(self, instream=None, infile=None, posix=False,
                 punctuation_chars=False):
        """
        instream	输入来源，可以是字符串或文件流
        infile	    输入文件名称，主要用于错误信息
        posix	    是否使用 POSIX 风格的解析模式
        punctuation_chars	是否将某些 Shell 标点符号作为独立 Token 处理
        """
        
        if isinstance(instream, str):
            # 包装成一个类似文件的内存流
            # from io import StringIO
            #
            # stream = StringIO("hello world")
            # print(stream.read(1))  # h
            # print(stream.read(1))  # e
            # print(stream.read(1))  # l
            # 
            # 包装为 StringIO 后，两种输入都能通过 read() 操作。
            instream = StringIO(instream)
        if instream is not None:
            self.instream = instream
            self.infile = infile
        else:
            # 如果 instream=None，它默认使用 sys.stdin
            self.instream = sys.stdin
            self.infile = None
        self.posix = posix

        # EOF 是 End Of File，表示输入已经结束。
        # 
        # POSIX 模式：   None 表示 EOF，因为在 POSIX 模式中，空字符串 '' 本身可以是一个合法的 Token。
        # 非 POSIX 模式：'' 表示 EOF。
        if posix:
            self.eof = None
        else:
            self.eof = ''

        # 定义各种字符的处理规则。
        # 
        # 注释起始字符
        self.commenters = '#'
        # 可以组成普通单词的字符
        self.wordchars = ('abcdfeghijklmnopqrstuvwxyz'
                          'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_')
        if self.posix:
            self.wordchars += ('ßàáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿ'
                               'ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞ')
        # 空白字符集合
        self.whitespace = ' \t\r\n'
        # 是否按空白字符进行分词
        self.whitespace_split = False

        # 单引号和双引号都会触发引用状态。
        self.quotes = '\'"'
        # 转义字符
        self.escape = '\\'
        # 在 POSIX 模式下允许使用反斜杠转义的引用类型
        self.escapedquotes = '"'

        # 当前词法分析状态
        self.state = ' '
        # 已读取但需要放回的 Token
        self.pushback = deque()
        # 当前行号，主要用于错误定位
        self.lineno = 1
        # 调试信息级别
        self.debug = 0
        # 当前正在构造的 Token
        self.token = ''
        # 输入源堆栈
        self.filestack = deque()
        # 输入源切换相关配置
        self.source = None

        # 配置 Shell 标点符号的分词规则。
        # False：禁用特殊标点分词。
        # True：使用默认集合 '();<>|&'。
        # str：使用用户指定的标点符号集合。
        if not punctuation_chars:
            punctuation_chars = ''
        elif punctuation_chars is True:
            punctuation_chars = '();<>|&'
        
        self._punctuation_chars = punctuation_chars

        # 在启用 punctuation_chars=True 后， 而且在 whitespace_split=False 的默认分词模式下：
        # 
        # echo hello && echo world 或者 echo hello&&echo world
        # 
        # && 会被识别为一个独立的 Token，即使它和其他字符之间没有空格。
        # 
        # ['echo', 'hello', '&&', 'echo', 'world']
        # 
        if punctuation_chars:
            # _pushback_chars is a push back queue used by lookahead logic
            # 字符级回退队列，用于前瞻（Lookahead）。
            # 如果提前读取的字符不属于当前 Token，
            # 可以先将它放回队列，留给下一次读取。
            self._pushback_chars = deque()
            # these chars added because allowed in file names, args, wildcards
            self.wordchars += '~-./*?='
            #remove any punctuation chars from wordchars

            # 假设 self.wordchars = "abc123_="  punctuation_chars = "_="
            # 
            # dict.fromkeys  将每个标点字符映射为 None。
            # 会得到 {'_': None, '=': None}
            # 
            # str.translate() 使用的转换表，通常需要以 Unicode 码点（整数） 作为 Key。
            # t = str.maketrans({
            #    "_": None,
            #    "=": None,
            # })
            # 得到了 
            # {
            #    95: None,
            #    61: None,
            # }
            t = self.wordchars.maketrans(dict.fromkeys(punctuation_chars))

            # 根据映射表转换字符串，映射值为 None 时删除字符；
            # 映射值为字符串或 Unicode 码点时执行替换；
            # 没有映射规则的字符保持不变。
            # 
            # 将指定的特殊标点从普通单词字符集合中移除，
            # 避免同一个字符同时属于 wordchars 和 punctuation_chars。
            # 例如 punctuation_chars="=" 时，确保 = 按特殊标点规则处理。
            self.wordchars = self.wordchars.translate(t)

    @property
    def punctuation_chars(self):
        return self._punctuation_chars

    def push_token(self, tok):
        "Push a token onto the stack popped by the get_token method"
        if self.debug >= 1:
            print("shlex: pushing token " + repr(tok))
        self.pushback.appendleft(tok)

    def push_source(self, newstream, newfile=None):
        "Push an input source onto the lexer's input source stack."
        if isinstance(newstream, str):
            newstream = StringIO(newstream)
        self.filestack.appendleft((self.infile, self.instream, self.lineno))
        self.infile = newfile
        self.instream = newstream
        self.lineno = 1
        if self.debug:
            if newfile is not None:
                print('shlex: pushing to file %s' % (self.infile,))
            else:
                print('shlex: pushing to stream %s' % (self.instream,))

    def pop_source(self):
        "Pop the input source stack."
        self.instream.close()
        (self.infile, self.instream, self.lineno) = self.filestack.popleft()
        if self.debug:
            print('shlex: popping to %s, line %d' \
                  % (self.instream, self.lineno))
        self.state = ' '

    def get_token(self):
        "Get a token from the input stream (or from stack if it's nonempty)"
        if self.pushback:
            tok = self.pushback.popleft()
            if self.debug >= 1:
                print("shlex: popping token " + repr(tok))
            return tok
        # No pushback.  Get a token.
        raw = self.read_token()
        # Handle inclusions
        if self.source is not None:
            while raw == self.source:
                spec = self.sourcehook(self.read_token())
                if spec:
                    (newfile, newstream) = spec
                    self.push_source(newstream, newfile)
                raw = self.get_token()
        # Maybe we got EOF instead?
        while raw == self.eof:
            if not self.filestack:
                return self.eof
            else:
                self.pop_source()
                raw = self.get_token()
        # Neither inclusion nor EOF
        if self.debug >= 1:
            if raw != self.eof:
                print("shlex: token=" + repr(raw))
            else:
                print("shlex: token=EOF")
        return raw

    def read_token(self):
        quoted = False
        escapedstate = ' '
        while True:
            if self.punctuation_chars and self._pushback_chars:
                nextchar = self._pushback_chars.pop()
            else:
                nextchar = self.instream.read(1)
            if nextchar == '\n':
                self.lineno += 1
            if self.debug >= 3:
                print("shlex: in state %r I see character: %r" % (self.state,
                                                                  nextchar))
            if self.state is None:
                self.token = ''        # past end of file
                break
            elif self.state == ' ':
                if not nextchar:
                    self.state = None  # end of file
                    break
                elif nextchar in self.whitespace:
                    if self.debug >= 2:
                        print("shlex: I see whitespace in whitespace state")
                    if self.token or (self.posix and quoted):
                        break   # emit current token
                    else:
                        continue
                elif nextchar in self.commenters:
                    self.instream.readline()
                    self.lineno += 1
                elif self.posix and nextchar in self.escape:
                    escapedstate = 'a'
                    self.state = nextchar
                elif nextchar in self.wordchars:
                    self.token = nextchar
                    self.state = 'a'
                elif nextchar in self.punctuation_chars:
                    self.token = nextchar
                    self.state = 'c'
                elif nextchar in self.quotes:
                    if not self.posix:
                        self.token = nextchar
                    self.state = nextchar
                elif self.whitespace_split:
                    self.token = nextchar
                    self.state = 'a'
                else:
                    self.token = nextchar
                    if self.token or (self.posix and quoted):
                        break   # emit current token
                    else:
                        continue
            elif self.state in self.quotes:
                quoted = True
                if not nextchar:      # end of file
                    if self.debug >= 2:
                        print("shlex: I see EOF in quotes state")
                    # XXX what error should be raised here?
                    raise ValueError("No closing quotation")
                if nextchar == self.state:
                    if not self.posix:
                        self.token += nextchar
                        self.state = ' '
                        break
                    else:
                        self.state = 'a'
                elif (self.posix and nextchar in self.escape and self.state
                      in self.escapedquotes):
                    escapedstate = self.state
                    self.state = nextchar
                else:
                    self.token += nextchar
            elif self.state in self.escape:
                if not nextchar:      # end of file
                    if self.debug >= 2:
                        print("shlex: I see EOF in escape state")
                    # XXX what error should be raised here?
                    raise ValueError("No escaped character")
                # In posix shells, only the quote itself or the escape
                # character may be escaped within quotes.
                if (escapedstate in self.quotes and
                        nextchar != self.state and nextchar != escapedstate):
                    self.token += self.state
                self.token += nextchar
                self.state = escapedstate
            elif self.state in ('a', 'c'):
                if not nextchar:
                    self.state = None   # end of file
                    break
                elif nextchar in self.whitespace:
                    if self.debug >= 2:
                        print("shlex: I see whitespace in word state")
                    self.state = ' '
                    if self.token or (self.posix and quoted):
                        break   # emit current token
                    else:
                        continue
                elif nextchar in self.commenters:
                    self.instream.readline()
                    self.lineno += 1
                    if self.posix:
                        self.state = ' '
                        if self.token or (self.posix and quoted):
                            break   # emit current token
                        else:
                            continue
                elif self.state == 'c':
                    if nextchar in self.punctuation_chars:
                        self.token += nextchar
                    else:
                        if nextchar not in self.whitespace:
                            self._pushback_chars.append(nextchar)
                        self.state = ' '
                        break
                elif self.posix and nextchar in self.quotes:
                    self.state = nextchar
                elif self.posix and nextchar in self.escape:
                    escapedstate = 'a'
                    self.state = nextchar
                elif (nextchar in self.wordchars or nextchar in self.quotes
                      or (self.whitespace_split and
                          nextchar not in self.punctuation_chars)):
                    self.token += nextchar
                else:
                    if self.punctuation_chars:
                        self._pushback_chars.append(nextchar)
                    else:
                        self.pushback.appendleft(nextchar)
                    if self.debug >= 2:
                        print("shlex: I see punctuation in word state")
                    self.state = ' '
                    if self.token or (self.posix and quoted):
                        break   # emit current token
                    else:
                        continue
        result = self.token
        self.token = ''
        if self.posix and not quoted and result == '':
            result = None
        if self.debug > 1:
            if result:
                print("shlex: raw token=" + repr(result))
            else:
                print("shlex: raw token=EOF")
        return result

    def sourcehook(self, newfile):
        "Hook called on a filename to be sourced."
        if newfile[0] == '"':
            newfile = newfile[1:-1]
        # This implements cpp-like semantics for relative-path inclusion.
        if isinstance(self.infile, str) and not os.path.isabs(newfile):
            newfile = os.path.join(os.path.dirname(self.infile), newfile)
        return (newfile, open(newfile, "r"))

    def error_leader(self, infile=None, lineno=None):
        "Emit a C-compiler-like, Emacs-friendly error-message leader."
        if infile is None:
            infile = self.infile
        if lineno is None:
            lineno = self.lineno
        return "\"%s\", line %d: " % (infile, lineno)

    def __iter__(self):
        # shlex 本身就是一个迭代器（Iterator）。
        # Iterable（可迭代对象）：能够通过 iter() 获取迭代器。
        # Iterator（迭代器）   ：能够通过 next() 逐个获取元素，并且自身也可迭代。
        return self

    def __next__(self):
        # 获取下一个token
        token = self.get_token()

        # 对应前面的
        # if posix:
        #     self.eof = None
        # else:
        #     self.eof = ''
        if token == self.eof:

            # StopIteration 是 Python 迭代器协议规定的结束信号。
            raise StopIteration
        return token

def split(s, comments=False, posix=True):
    """Split the string *s* using shell-like syntax.
    s	         必填	   需要分词的字符串
    comments	 False	是否将注释符识别为注释起始
    posix	     True	是否启用 POSIX 风格的 Shell 解析规则

    curl -H 'Content-Type: application/json'

    会被分词为 ['curl', '-H', 'Content-Type: application/json']
    """

    # 兼容旧版本允许传入 None 的行为，并发出弃用警告。
    if s is None:
        import warnings
        warnings.warn("Passing None for 's' to shlex.split() is deprecated.",
                      DeprecationWarning, stacklevel=2)

    # 实例化一个 shlex 类对象。
    lex = shlex(instream=s, posix=posix)

    # 使用空白字符作为 Token 的分隔依据，同时遵守引号和转义规则。
    lex.whitespace_split = True

    # 默认情况下，shlex 使用 # 作为注释起始字符。
    # 没有开启注释的时候，那么就关闭注释识别。
    if not comments:
        lex.commenters = ''

    # 通过迭代器协议逐个获取 Token，并将结果转换为列表。
    return list(lex)


# 将 Token 序列重新组合为 Shell 命令，可以理解为 split() 的逆向操作。
# 注意：不保证恢复原始命令的引号格式，只保证参数含义。
def join(split_command):
    """Return a shell-escaped string from *split_command*."""
    return ' '.join(quote(arg) for arg in split_command)

# 判断单个参数是否需要 Shell 引号保护。
# 匹配不在以下安全字符集合中的任意字符：
# \w	       英文字母、数字、下划线（在 re.ASCII 下）
# @%+=:,./-	   字符
# 
_find_unsafe = re.compile(r'[^\w@%+=:,./-]', re.ASCII).search


def quote(s):
    """Return a shell-escaped version of the string *s*."""

    # 空字符串必须使用 '' 表示，否则 Shell 会认为不存在该参数。
    if not s:
        return "''"

    # 没有不安全字符，直接返回
    if _find_unsafe(s) is None:
        return s

    # use single quotes, and put single quotes into double quotes
    # the string $'b is then quoted as '$'"'"'b'
    # 
    # 使用单引号保护整个字符串，避免 Shell 解释特殊字符。
    #
    # 单引号内部无法直接包含单引号，因此需要：
    # 1. 结束当前单引号
    # 2. 使用双引号表示一个字面量单引号
    # 3. 重新开启单引号
    #
    # 例如：
    # it's ok  ->  'it'"'"'s ok'
    # 
    # 其中 
    # 
    # 'it'   - it
    # "'"    - '
    # 's ok' - s ok
    #
    # Shell 中，没有未被引用的分隔符隔开时，相邻的字符串片段会组成同一个参数，而不是自动插入空格。
    # 'hello''world'	  helloworld
    # 'hello' 'world'	  两个参数：hello、world
    # 'hello' "world"	  两个参数：hello、world
    # 'hello'"world"	  helloworld
    # 'hello' ' world'	  两个参数：hello、 world
    # 'hello' " world"   （无中间空格）	hello world
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _print_tokens(lexer):
    while 1:
        tt = lexer.get_token()
        if not tt:
            break
        print("Token: " + repr(tt))

if __name__ == '__main__':
    if len(sys.argv) == 1:
        _print_tokens(shlex())
    else:
        fn: str = sys.argv[1]
        with open(fn) as f:
            _print_tokens(shlex(f, fn))
