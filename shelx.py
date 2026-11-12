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

        # 当前词法分析状态，初始状态是 ' '
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
        # 优先读取通过 push_token() 放回的 Token。
        # push_token() 使用 appendleft()，因此最近放回的 Token 会最先返回。
        if self.pushback:
            tok = self.pushback.popleft()
            if self.debug >= 1:
                print("shlex: popping token " + repr(tok))
            return tok

        # No pushback.  Get a token.
        # 从read_token 拿到
        raw = self.read_token()

        # Handle inclusions
        # 这里的source 作用是 shlex 自己提供的一种输入源包含机制
        # 允许词法分析器在读取一个输入源时，暂时切换到另一个输入源。
        # 假设 lex.source = "source" 那么遇到 source 这个 Token 时，shlex 会尝试将后面的 Token 当成一个输入源名称。
        # 
        # 比如要处理的文本字段是 hello source extra.txt world
        # 如果 sourcehook() 成功打开 extra.txt，就可以先读取其中的内容，再回来继续读取 world。
        if self.source is not None:
            # 发现有source
            while raw == self.source:
                # 那么就让下一个token作为source输入到sourcehook里面
                spec = self.sourcehook(self.read_token())
                if spec:
                    (newfile, newstream) = spec
                    self.push_source(newstream, newfile)
                # 递归调用 get_token()，继续获取下一个有效 Token。
                # 如果成功切换输入源，就从新输入源读取。
                raw = self.get_token()


        # Maybe we got EOF instead?
        while raw == self.eof:
            # 如果当前输入流结束（filestack用于保存之前的输入源。）
            # - filestack 为空：说明没有其他输入源需要恢复，返回 EOF。
            # - filestack 非空：说明当前只是嵌套输入源结束，需要恢复上一层输入源。
            if not self.filestack:
                return self.eof
            else:
                ## 递归调用了这里
                # get_token()
                #    │
                #    ├── 发现 source a.txt
                #    │
                #    └── get_token()
                #            │
                #            ├── 发现 source b.txt
                #            │
                #            └── get_token()
                #                    │
                #                    └── 读取 b.txt 的 Token
                # 当前输入源结束，恢复上一层输入源。
                self.pop_source()
                # 递归调用，继续从恢复后的输入源获取 Token。
                raw = self.get_token()


        # Neither inclusion nor EOF
        # 输出调试信息。
        if self.debug >= 1:
            if raw != self.eof:
                print("shlex: token=" + repr(raw))
            else:
                print("shlex: token=EOF")
        return raw

    def read_token(self):
        # 当前正在生成的 Token 是否经过引号引用。
        # - self.state：当前处于什么解析状态。
        # - quoted：当前 Token 是否曾经进入过引号状态。
        # 
        # 比如 echo '' 
        # 其中 '' 代表一个真正存在的空参数。
        # 由于空字符串的布尔值为 False，解析器就可能把这个合法的空参数丢掉。
        # 
        # 
        quoted = False

        
        # 记录进入转义状态之前，处理完成后应该恢复到哪个状态。
        # 比如 echo hello\ world
        # 
        # 1. 当前处于普通单词状态 'a'。
        # 2. 遇到 \，进入转义状态。
        # 3. 将 escapedstate 保存为 'a'。
        # 4. 读取被转义的空格，将它添加到当前 Token。
        # 5. 恢复状态 'a'，继续读取。
        # 
        escapedstate = ' '

        # 循环会不断读取字符，直到：
        # - 识别出一个完整 Token。
        # - 遇到 EOF。
        # - 遇到无法处理的引用或转义错误。
        while True:

            # 如果启用了标点处理，而且回退缓冲区中存在字符，就优先取出。
            # 否则从输入流中读取一个新字符。
            if self.punctuation_chars and self._pushback_chars:
                nextchar = self._pushback_chars.pop()
            else:
                nextchar = self.instream.read(1)

            # 每当本轮取得的字符为换行符时，行号加一。
            # 字符可能来自输入流，也可能来自字符回退缓冲区。
            if nextchar == '\n':
                self.lineno += 1
            
            # 调试输出,当调试等级至少为 3 时，打印：
            # - 当前状态 self.state。
            # - 当前读取的字符 nextchar。
            if self.debug >= 3:
                print("shlex: in state %r I see character: %r" % (self.state,
                                                                  nextchar))

            """
            状态	          含义	               示例
            None	     输入已经结束	              EOF
            ' '	         等待开始新的 Token	    空白分隔区
            ' 或 "	    正在读取引用字符串	   'hello world'
            \	         正在处理转义字符        hello\ world
            'a'	         正在读取普通单词	        hello
            'c'	         正在读取连续特殊标点	     &&、\|\|
            """

            # 词法分析器已经到达 EOF
            if self.state is None:
                # 清空当前 Token。
                self.token = ''        # past end of file
                break

            # 当前处于 Token 之间，等待新 Token 开始
            elif self.state == ' ':
                # 下一个nextchar 为 None或者为 空字符串
                if not nextchar:
                    self.state = None  # end of file
                    break
                # 如果下一个字符是空白字符
                # 识别这几种： ' \t\r\n'
                elif nextchar in self.whitespace:
                    if self.debug >= 2:
                        print("shlex: I see whitespace in whitespace state")

                    # 如果已经构造出 Token，或者在 POSIX 模式下已经处理过引号，就结束当前 Token。否则继续跳过空白字符。
                    if self.token or (self.posix and quoted):
                        break   # emit current token
                    else:
                        continue                                                                                                

                # commenters=#
                elif nextchar in self.commenters:
                    # 只要读取到了 注释读取一行
                    self.instream.readline()
                    self.lineno += 1

                # 当前词法分析器正处于等待新 Token 开始的状态。
                # 其中 self.escape = '\\'
                # 
                elif self.posix and nextchar in self.escape:
                    escapedstate = 'a'

                    # self.state = '\\' 下一轮循环会进入 elif self.state in self.escape:
                    self.state = nextchar

                # 普通单词字符
                elif nextchar in self.wordchars:
                    self.token = nextchar
                     # 进入普通单词状态，后续继续构造 Token。
                    self.state = 'a'

                # 如果启用了标点符号解析，且当前字符属于指定的标点集合，
                # 则以该字符开始一个新的 Token。
                elif nextchar in self.punctuation_chars:
                    self.token = nextchar
                    # 进入连续标点符号处理状态。
                    self.state = 'c'

                # self.quotes = '\'"'
                elif nextchar in self.quotes:
                    # 非 POSIX 模式会保留起始引号。
                    # 当前使用 posix=True，因此不执行。
                    if not self.posix:
                        self.token = nextchar
                        # 使用当前引号字符作为状态标识。
                    self.state = nextchar

                # 例如，在 self.state == ' ' 的情况下读取到 @：
                # 1. @ 不属于默认的 wordchars。
                # 2. 没有启用 punctuation_chars。
                # 3. @ 也不是引号、空白字符或转义符。
                # 4. 因此进入 whitespace_split 分支。
                # 
                # 将前面分支未处理的字符作为新 Token 的起始字符。
                elif self.whitespace_split:
                    self.token = nextchar
                    self.state = 'a'
                # 主要用于 whitespace_split=False 的情况。
                else:
                    # 将当前字符设置为 Token。
                    self.token = nextchar

                    # 如果当前 Token 非空，就执行 break，退出 while True。
                    # 即使 Token 是空字符串，如果它来自 POSIX 模式下的引用内容，也可能是一个合法的空 Token。
                    if self.token or (self.posix and quoted):
                        break   # emit current token
                    else:
                        continue
            elif self.state in self.quotes:
                # 标记已经进入了引号状态
                quoted = True

                # 引号没有正常闭合
                if not nextchar:      # end of file
                    if self.debug >= 2:
                        print("shlex: I see EOF in quotes state")
                    # XXX what error should be raised here?
                    raise ValueError("No closing quotation")

                # 如果当前处于单引号状态，self.state = "'"。
                # 如果当前处于双引号状态，self.state = '"'。
                if nextchar == self.state:
                    if not self.posix:
                        self.token += nextchar
                        self.state = ' '
                        break
                    # 在 posix 状态下，状态会变成 a
                    else:
                        self.state = 'a'

                # self.posix == True
                # 当前字符是反斜杠 \
                # 
                #  self.escapedquotes = '"' 
                # 所以 self.state 是双引号，同时当前状态是单引号；
                elif (self.posix and nextchar in self.escape and self.state
                      in self.escapedquotes):
                          
                    escapedstate = self.state
                    self.state = nextchar
               
                # 如果当前字符既不是结束引号，也没有触发转义处理，就直接追加到当前 Token。
                else:
                    self.token += nextchar

            # self.escape = '\\'， 当前处于反斜杠转义状态
            elif self.state in self.escape:
                # 检查 EOF
                if not nextchar:      # end of file
                    if self.debug >= 2:
                        print("shlex: I see EOF in escape state")
                    # XXX what error should be raised here?
                    raise ValueError("No escaped character")
                
                # In posix shells, only the quote itself or the escape
                # character may be escaped within quotes.
                # 
                # self.state：当前是反斜杠状态，即 '\\'。
                # escapedstate：进入转义状态前记录的恢复状态，可能是 'a' 或 '"'
                if (
                    
                    escapedstate in self.quotes and
                        # 下一个字符不是反斜杠
                        nextchar != self.state and 
                        # 下一个字符不是当前引号字符
                        nextchar != escapedstate):
                    self.token += self.state
                
                self.token += nextchar
                # 返回转义前指定的状态。
                self.state = escapedstate

            
            elif self.state in ('a', 'c'):
                # 读取到 EOF，结束词法分析。
                if not nextchar:
                    self.state = None   # end of file
                    break

                # 下一个字符是空白字符
                elif nextchar in self.whitespace:
                    if self.debug >= 2:
                        print("shlex: I see whitespace in word state")
                    # 当前状态变成空格
                    self.state = ' '
                    # 如果当前 Token 非空，或者是合法的空引用 Token，
                    # 则结束本轮读取。
                    if self.token or (self.posix and quoted):
                        break   # emit current token
                    else:
                        continue

                # 判断下一个字符是否是注释
                # 目前的注释默认是 ''
                elif nextchar in self.commenters:
                    self.instream.readline()
                    self.lineno += 1
                    # 
                    if self.posix:
                        self.state = ' '
                        # 如果形成了有效的token, 那么就退出
                        if self.token or (self.posix and quoted):
                            break   # emit current token
                        else:
                            continue

                # 连续标点符号状态
                elif self.state == 'c':
                    # 下一个字符是标点符号
                    if nextchar in self.punctuation_chars:
                        self.token += nextchar
                    else:
                        # 同时下一个字符也不是空白
                        if nextchar not in self.whitespace:
                            # 假设下一个不是空白也不是符号，那么一般是字符，需要保存起来
                            self._pushback_chars.append(nextchar)
                        # 不是标点符号 state = ' '   
                        self.state = ' '
                        break
                                             
                # 如果下一个字符是引号，状态变成引号
                elif self.posix and nextchar in self.quotes:
                    self.state = nextchar

                # 下一个字符是转义符
                elif self.posix and nextchar in self.escape:
                    escapedstate = 'a'
                    self.state = nextchar
  
                # 当前字符属于普通单词字符或引号，
                # 或者启用了空白分词且当前字符不是特殊标点，
                # 则将其直接追加到当前 Token。
                elif (nextchar in self.wordchars or
                    nextchar in self.quotes or 
                    (self.whitespace_split and
                    nextchar not in self.punctuation_chars)):
                        
                    self.token += nextchar

                # 当前字符不能继续追加到 Token，需要结束当前 Token，同时保留刚刚读取的字符。
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
