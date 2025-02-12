from ipykernel.kernelbase import Kernel
from pexpect import replwrap, EOF
import pexpect

from subprocess import check_output
import os.path

import re
import signal

import os

from .sessions import FifoWolframscriptSession

# https://stackoverflow.com/a/20885799/10155767
try:
    import importlib.resources as pkg_resources
except ImportError:
    # Try backported to PY<37 `importlib_resources`.
    import importlib_resources as pkg_resources

# W completions
WNames = pkg_resources.read_text(__package__, 'Names.wl.txt').split()


__version__ = '0.7.1'

version_pat = re.compile(r'version (\d+(\.\d+)+)')

from .images import (
    extract_image_filenames, display_data_for_image, image_setup_cmd
)

class IREPLWrapper(replwrap.REPLWrapper):
    """A subclass of REPLWrapper that gives incremental output
    specifically for bash_kernel.

    The parameters are the same as for REPLWrapper, except for one
    extra parameter:

    :param line_output_callback: a callback method to receive each batch
      of incremental output. It takes one string parameter.
    """
    def __init__(self, cmd_or_spawn, orig_prompt, prompt_change,
                 extra_init_cmd=None, line_output_callback=None):
        self.line_output_callback = line_output_callback
        replwrap.REPLWrapper.__init__(self, cmd_or_spawn, orig_prompt,
                                      prompt_change, extra_init_cmd=extra_init_cmd)

    def _expect_prompt(self, timeout=-1):
        if timeout == None:
            # "None" means we are executing code from a Jupyter cell by way of the run_command
            # in the do_execute() code below, so do incremental output.
            while True:
                pos = self.child.expect_exact([self.prompt, self.continuation_prompt, u'\r\n'],
                                              timeout=None)
                if pos == 2:
                    # End of line received
                    self.line_output_callback(self.child.before + '\n')
                else:
                    if len(self.child.before) != 0:
                        # prompt received, but partial line precedes it
                        self.line_output_callback(self.child.before)
                    break
        else:
            # Otherwise, use existing non-incremental code
            pos = replwrap.REPLWrapper._expect_prompt(self, timeout=timeout)

        # Prompt received, so return normally
        return pos

class BashKernel(Kernel):
    implementation = 'JWLS_kernel'
    implementation_version = __version__

    @property
    def language_version(self):
        m = version_pat.search(self.banner)
        return m.group(1)

    _banner = None

    @property
    def banner(self):
        if self._banner is None:
            self._banner = check_output(['bash', '--version']).decode('utf-8')
        return self._banner

    language_info = {'name': 'IWLS',
                     'codemirror_mode': 'mathematica',
                     'mimetype': 'text/x-mathematica',
                     'file_extension': '.wl'}

    def __init__(self, **kwargs):
        Kernel.__init__(self, **kwargs)
        self._start_wolframscript()
        self._start_bash()

    def _start_wolframscript(self):
        self.wolframscript = FifoWolframscriptSession()
        self.temp_path = str(self.wolframscript.temp_path)

    def do_shutdown(self, restart):
        self.wolframscript.close()

    def _start_bash(self):
        # Signal handlers are inherited by forked processes, and we can't easily
        # reset it from the subprocess. Since kernelapp ignores SIGINT except in
        # message handlers, we need to temporarily reset the SIGINT handler here
        # so that bash and its children are interruptible.
        pexpect.spawn(f"echo Temp path: {self.temp_path}")
        sig = signal.signal(signal.SIGINT, signal.SIG_DFL)
        try:
            # Note: the next few lines mirror functionality in the
            # bash() function of pexpect/replwrap.py.  Look at the
            # source code there for comments and context for
            # understanding the code here.
            bashrc = os.path.join(os.path.dirname(pexpect.__file__), 'bashrc.sh')
            child = pexpect.spawn("bash", ['--rcfile', bashrc], echo=False,
                                  encoding='utf-8', codec_errors='replace')
            ps1 = replwrap.PEXPECT_PROMPT[:5] + u'\[\]' + replwrap.PEXPECT_PROMPT[5:]
            ps2 = replwrap.PEXPECT_CONTINUATION_PROMPT[:5] + u'\[\]' + replwrap.PEXPECT_CONTINUATION_PROMPT[5:]
            prompt_change = u"PS1='{0}' PS2='{1}' PROMPT_COMMAND=''".format(ps1, ps2)

            # Using IREPLWrapper to get incremental output
            self.bashwrapper = IREPLWrapper(child, u'\$', prompt_change,
                                            extra_init_cmd="export PAGER=cat",
                                            line_output_callback=self.process_output)
        finally:
            signal.signal(signal.SIGINT, sig)

        # Register Bash function to write image data to temporary file
        self.bashwrapper.run_command(image_setup_cmd)

    def process_output(self, output):
        if not self.silent:
            image_filenames, output = extract_image_filenames(output)

            # Send standard output
            stream_content = {'name': 'stdout', 'text': output}
            self.send_response(self.iopub_socket, 'stream', stream_content)

            # Send images, if any
            for filename in image_filenames:
                try:
                    data = display_data_for_image(filename)
                except ValueError as e:
                    message = {'name': 'stdout', 'text': str(e)}
                    self.send_response(self.iopub_socket, 'stream', message)
                else:
                    self.send_response(self.iopub_socket, 'display_data', data)


    def do_execute(self, code, silent, store_history=True,
                   user_expressions=None, allow_stdin=False):
        self.silent = silent
        if not code[0] =='!':
            # Pipe wl code into the fifo
            code = f"echo '{code}'> '{self.temp_path}/wlin.fifo'"
        else:
            code = code[1:]
        if not code.strip():
            return {'status': 'ok', 'execution_count': self.execution_count,
                    'payload': [], 'user_expressions': {}}

        interrupted = False
        try:
            # empty the WolframScript log file
            self.bashwrapper.run_command(f"echo 'JWLSemptylogF' > '{self.temp_path}/wlin.fifo'", timeout=None)
            # auxiliary file to check
            self.bashwrapper.run_command(f"cat '{self.temp_path}/wlout.txt' > '{self.temp_path}/wlout2' ", timeout=None)

            # Note: timeout=None tells IREPLWrapper to do incremental
            # output.  Also note that the return value from
            # run_command is not needed, because the output was
            # already sent by IREPLWrapper.
            self.bashwrapper.run_command(code.rstrip(), timeout=None)
            # pipe the latest outputs to  .wlout.txt 
            self.bashwrapper.run_command(f"echo 'JWLScatoutF' > '{self.temp_path}/wlin.fifo' ", timeout=None)
            # show last outputs
            self.bashwrapper.run_command(f"while cmp -s '{self.temp_path}/wlout.txt' '{self.temp_path}/wlout2' ; do sleep 0.1 ; done ; sleep 0.1 ; tail -n +2 '{self.temp_path}/wlout.txt' | grep . | sed '0~1 a\\\'", timeout=None)
            # self.bashwrapper.run_command(f"cat '{self.temp_path}/wlout.txt'", timeout=None)            
            
        except KeyboardInterrupt:
            self.bashwrapper.child.sendintr()
            interrupted = True
            self.bashwrapper._expect_prompt()
            output = self.bashwrapper.child.before
            self.process_output(output)
        except EOF:
            output = self.bashwrapper.child.before + 'Restarting Bash'
            self._start_bash()
            self.process_output(output)

        if interrupted:
            return {'status': 'abort', 'execution_count': self.execution_count}

        try:
            exitcode = int(self.bashwrapper.run_command('echo $?').rstrip())
        except Exception:
            exitcode = 1

        if exitcode:
            error_content = {'execution_count': self.execution_count,
                             'ename': '', 'evalue': str(exitcode), 'traceback': []}

            self.send_response(self.iopub_socket, 'error', error_content)
            error_content['status'] = 'error'
            return error_content
        else:
            return {'status': 'ok', 'execution_count': self.execution_count,
                    'payload': [], 'user_expressions': {}}

    def do_complete(self, code, cursor_pos):
        code = code[:cursor_pos]
        default = {'matches': [], 'cursor_start': 0,
                   'cursor_end': cursor_pos, 'metadata': dict(),
                   'status': 'ok'}

        if not code or code[-1] == ' ':
            return default

        tokens = code.replace(';', ' ').replace('@', ' ').replace('/', ' '
                    ).replace('?', ' ').replace(',', ' ').replace('.', ' '
                    ).replace('>', ' ').replace('<', ' ').replace(':', ' '
                    ).replace('[', ' ').replace(']', ' ').replace('(', ' '
                    ).replace(')', ' ').replace('{', ' ').replace('}', ' '
                    ).replace('_', ' ').replace('+', ' ').replace('*', ' '
                    ).replace('#', ' ').replace('=', ' ').replace('"', ' '
                    ).replace('~', ' ').replace('&', ' ').replace('|', ' '
                    ).replace('!', ' ').split()
        if not tokens:
            return default

        matches = WNames
        token = tokens[-1]
        start = cursor_pos - len(token)

        if token[0] == '$':
            # complete variables
            cmd = 'compgen -A arrayvar -A export -A variable %s' % token[1:] # strip leading $
            output = self.bashwrapper.run_command(cmd).rstrip()
            completions = set(output.split())
            # append matches including leading $
            matches.extend(['$'+c for c in completions])
        else:
            # complete functions and builtins
            cmd = 'compgen -cdfa %s' % token
            output = self.bashwrapper.run_command(cmd).rstrip()
            matches.extend(output.split())

        if not matches:
            return default
        matches = [m for m in matches if m.startswith(token)]

        return {'matches': sorted(matches), 'cursor_start': start,
                'cursor_end': cursor_pos, 'metadata': dict(),
                'status': 'ok'}
