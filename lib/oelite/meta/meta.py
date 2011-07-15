import sys
import string
import os
import re
import codeop
import warnings
import hashlib
from oebakery import die, err, warn, info, debug
from collections import MutableMapping
from oelite.meta import *
from oelite.pyexec import *
import oelite.function


class ExpansionError(Exception):

    def __init__(self, msg, stack):
        if isinstance(msg, Exception):
            self.msg = "%s: %s"%(msg.__class__.__name__, str(msg))
        else:
            self.msg = str(msg)
        self.stack = stack

    def __repr__(self):
        return "%s(%s)"%(self.__class__.__name__, repr(self.msg))

    def __str__(self):
        return self.msg

    def print_details(self):
        if len(self.stack) == 0:
            return
        print "Expansion stack:\n%s"%(str(self.stack))


class ExpansionStack:

    def __init__(self):
        self.stack = []
        self.python = False
        return

    def push(self, var):
        if var in self.stack:
            raise Exception("Circular expansion: %s"%("->".join(self.stack)))
        self.stack.append(var)
        return

    def pop(self):
        del self.stack[-1:]
        return

    def __len__(self):
        return len(self.stack)

    def __str__(self, prefix="  "):
        return prefix + ("\n%s"%(prefix)).join(self.stack)


pythonfunc_code_cache = {}
    

class MetaData(MutableMapping):


    def __init__(self, meta=None):
        super(MetaData, self).__init__()
        if meta is None:
            pass
        elif isinstance(meta, dict):
            self.import_dict(meta)
        elif "dict" in dir(meta):
            self.import_dict(meta.dict())
        else:
            raise Exception("invalid argument: meta=%s"%(repr(meta)))
        self.pythonfunc_init()
        self.expand_stack = ExpansionStack()
        return


    def import_dict(self, d):
        for key in d:
            if key == "__file_mtime":
                for (filename, mtime) in d[key][""]:
                    self.set_file_mtime(filename, mtime)
                continue
            for (flag, value) in d[key].items():
                #print "importing %s[%s]=%s"%(key,flag,value)
                if flag:
                    self.set_flag(key, flag, value)
                else:
                    self.set(key, value)
        return


    def __repr__(self):
        return '%s()'%(self.__class__.__name__)

    def __eq__(self):
        raise Exception("__eq__() not implemented")

    def __hash__(self):
        raise Exception("__hash__() not implemented")

    def __nonzero__(self):
        raise Exception("__nonzero__() not implemented")

    def __len__(self): # required by Sized
        raise Exception("__len__() not implemented")

    def __getitem__(self, key): # required by Mapping
        return self.get(key, 0)

    def __setitem__(self, key, value): # required by MutableMapping
        self.set(key, value)
        return value

    def __delitem__(self, key): # required by MutableMapping
        self.del_var(key)
        return

    def __iter__(self): # required by Iterable
        return self.get_vars().__iter__()

    def __reversed__(self):
        raise Exception("__reversed__() not implemented")

    def __contains__(self, item): # required by Container
        val = self.get(item, False)
        return val is not None


    def get_list(self, var, **kwargs):
        return (self.get(var, **kwargs) or "").split()

    #def get_flag_list(self, var, flag, **kwargs):
    def get_list_flag(self, var, flag, **kwargs):
        return (self.get_flag(var, flag, **kwargs) or "").split()

    def get_boolean_flag(self, var, flag, **kwargs):
        return bool(self.get_flag(var, flag, **kwargs))


    def pythonfunc_init(self):
        self.pythonfunc_cache = {}
        imports = (self.get("OE_IMPORTS", expand=False) or "")
        g = {}
        g["__builtins__"] = globals()["__builtins__"]
        for module_name in imports.split():
            # FIXME: debug this, optimizing it so that we don't import
            # stuff more than absolutely necessary
            #print "importing module", module_name
            base_name = module_name.split(".")[0]
            g[base_name] = __import__(module_name, g, [], [], 0)
        self.pythonfunc_globals = g
        return


    def get_pythonfunc_globals(self):
        g = self.pythonfunc_globals.copy()
        return g


    #def get_autoimport_pythonfuncs(self, g=None):
    #    return self.get_pythonfuncs(self.get_vars(flag="autoimport"), g)


    def get_pythonfuncs(self, functions, g=None):
        pythonfuncs = {}
        for function in functions:
            pythonfuncs[function] = self.get_pythonfunc(function, g)
        return pythonfuncs


    def get_pythonfunc(self, var, name=None, tmpdir=None,
                       set_ld_library_path=True):
        #if function in self.pythonfunc_cache:
        #    return self.pythonfunc_cache[function]
        function = oelite.function.PythonFunction(
            self, var, name=name, tmpdir=tmpdir,
            set_ld_library_path=set_ld_library_path)
        #self.pythonfunc_cache[function] = function
        return function


    def get_pythonfunc_code(self, var):
        if not self.get_flag(var, "python"):
            raise Exception("%s is not a python function"%(var))
        filename = self.get_flag(var, "filename") or "?"
        lineno = self.get_flag(var, "lineno") or "1"
        args = self.get_flag(var, "args") or ""
        body = self.get(var, expand=False)
        if not body or body.strip() == "":
            body = "    pass"
        source = "def %s(%s):\n%s\n"%(var, args, body)
        newlines = "\n" * (lineno - 1)
        if source in pythonfunc_code_cache:
            return pythonfunc_code_cache[source]
        try:
            code = codeop.compile_command(newlines + source, filename)
        except SyntaxError, e:
            print "Syntax error in python function: %s"%(var)
            print e
            #print source
            raise
        if not code:
            raise Exception("%s is not valid Python code"%(var))
        pythonfunc_code_cache[source] = code
        return code


    def expand(self, string, method=FULL_EXPANSION):
        """Expand string using variable data.
    
        Arguments:
        string -- String to expand
        method -- Expansion method (default: FULL_EXPANSION)
    
        Expansion methods:
        NO_EXPANSION -- no recursive expansion
        FULL_EXPANSION -- full expansion, all variables must be expanded
        PARTIAL_EXPANSION -- partial, allow unknown variables to remain unexpanded
        CLEAN_EXPANSION -- clean, expand unknown variables to empty string
        """
        #print "expand method=%s string=%s"%(method, repr(string))
        assert isinstance(method, int)
        (new_string, deps) = self._expand(string, method)
        return new_string

    def _expand(self, string, method, var=None):
        #print "_expand method=%s string=%s"%(method, repr(string))
        assert isinstance(method, int)
        orig_string = string
        var_re    = re.compile(r"\${[^@{}]+}")
        python_re = re.compile(r"\${@.+?}")
        deps = set()
        expanded_string = ""
        string_ptr = 0
        for var_match in var_re.finditer(string):
            var = var_match.group(0)[2:-1]
            (val, recdeps) = self._get(var)
            if val is None:
                if method == CLEAN_EXPANSION:
                    val = ""
                elif method == FULL_EXPANSION:
                    raise ExpansionError("Cannot expand variable ${%s}"%(var),
                                         self.expand_stack)
            expanded_string += (string[string_ptr:var_match.start(0)] +
                                "%s"%(val,))
            string_ptr = var_match.end(0)
            deps.add(var)
            if recdeps:
                deps.union(recdeps)
        expanded_string += string[string_ptr:]
        python_match = python_re.search(expanded_string)
        if python_match:
            python_source = python_match.group(0)[3:-1]
            self.expand_stack.push("${@%s}"%(str(python_source)))
            python_output = inlineeval(python_source, self, var)
            (expanded_output, recdeps) = self._expand(python_output, method)
            expanded_string = (expanded_string[:python_match.start(0)] +
                               expanded_output +
                               expanded_string[python_match.end(0):])
            deps.add("python")
            if recdeps:
                deps.union(recdeps)
            self.expand_stack.pop()
        #print "returning expanded string %s"%(repr(expanded_string))
        return (expanded_string, deps)


    def append(self, var, value, separator=""):
        current = self.get(var, NO_EXPANSION)
        #print "append current=%s value=%s"%(repr(current), repr(value))
        if current is None:
            self.set(var, value)
        else:
            self.set(var, current + separator + value)

    def append_flag(self, var, flag, value, separator=""):
        current = self.get_flag(var, flag)
        if current is None:
            self.set_flag(var, flag, value)
        else:
            self.set_flag(var, flag, current + separator + value)

    def append_override(self, var, override, value, separator=""):
        current = self.get_override(var, override)
        if current is None:
            self.set_override(var, override, value)
        else:
            self.set_override(var, override, current + separator + value)

    def prepend(self, var, value, separator=""):
        current = self.get(var, NO_EXPANSION)
        if current is None:
            self.set(var, value)
        else:
            self.set(var, value + separator + current)

    def prepend_flag(self, var, flag, value, separator=""):
        current = self.get_flag(var, flag)
        if current is None:
            self.set_flag(var, flag, value)
        else:
            self.set_flag(var, flag, value + separator + current)

    def prepend_override(self, var, override, value, separator=""):
        current = self.get_override(var, override)
        if current is None:
            self.set_override(var, override, value)
        else:
            self.set_override(var, override, value + separator + current)


    def setVar(self, var, val):
        #warnings.warn("setVar() is deprecated, use set()")
        return self.set(var, val)


    def setVarFlag(self, var, flag, val):
        #warnings.warn("setVarFlag() is deprecated, use set_flag()")
        return self.set_flag(var, flag, val)


    def getVar(self, var, expand=FULL_EXPANSION):
        #warnings.warn("getVar() is deprecated, use get()")
        rv = self.get(var, expand)
        #print "getVar: %s=%s"%(var, repr(rv))
        return rv


    def getVarFlag(self, var, flag, expand=False):
        #warnings.warn("getVarFlag() is deprecated, use get_flag()")
        return self.get_flag(var, flag, expand)


    def delVar(self, var):
        #warnings.warn("delVar() is deprecated, use del built-in")
        del self[var]
        return


    builtin_nohash = [
        "OE_REMOTES",
        "OE_MODULES",
        "BB_ENV_WHITELIST",
        "PATH",
        "PWD",
        "SHELL",
        "TERM",
        "TOPDIR",
        "TMPDIR",
        "BBPATH",
        "BBPATH_PRETTY",
        "BBRECIPES",
        "BBRECIPES_PRETTY",
        "FILE",
        "_task_deps",
    ]
    
    builtin_nohash_prefix = [
        "OE_REMOTE_",
        "OE_MODULE_",
    ]
   
    
    def dump_var(self, key, o=sys.__stdout__, pretty=True, dynvars={}):
        if pretty:
            eol = "\n\n"
        else:
            eol = "\n"
    
        expand = self.get_flag(key, "expand")
        if expand is not None:
            expand = int(expand)
        else:
            if self.get_flag(key, "python"):
                expand = False
            else:
                expand = True
        val = self.get(key, expand)
    
        if not val:
            return 0
    
        val = str(val)
    
        for varname in dynvars.keys():
            val = string.replace(val, dynvars[varname], "${%s}"%(varname))
    
        if self.get_flag(key, "func"):
            o.write("%s() {\n%s}%s"%(key, val, eol))
            return
    
        if pretty:
            o.write("# %s=%s\n"%(key, self.get(key, False)))
        if self.get_flag(key, "export"):
            o.write("export ")
        
        o.write("%s=%s%s"%(key, repr(val), eol))
        return


    def dump(self, o=sys.__stdout__, pretty=True, nohash=False, only=None):

        dynvars = {}
        for varname in ("WORKDIR", "TOPDIR", "DATETIME"):
            dynvars[varname] = self.get(varname, True) or None

        keys = sorted((key for key in self.keys() if not key.startswith("__")))
        for key in keys:
            if only and key not in only:
                continue
            if not nohash:
                if key in self.builtin_nohash:
                    continue
                if self.get_flag(key, "nohash"):
                    continue
                nohash_prefixed = False
                for prefix in self.builtin_nohash_prefix:
                    if key.startswith(prefix):
                        nohash_prefixed = True
                        break
                if nohash_prefixed:
                    continue
            self.dump_var(key, o, pretty, dynvars)


    def get_function(self, name):
        if not name in self or not self.get(name):
            return oelite.function.NoopFunction(self, name)
        if self.get_flag(name, "python"):
            return oelite.function.PythonFunction(self, name)
        else:
            return oelite.function.ShellFunction(self, name)
