#-----------------------------------------------------------------------------------
# UnrealScriptIDE Main: Auto-complete Plug-in
#-----------------------------------------------------------------------------------
#
#   This plug-in displays classes, variables and Functions with parameters
#   and inserts them as dynamic snippets.
#   It searches in all parent classes of the current class.
#   Uses context sensitive completions.
#
#
# (c) Florian Zinggeler
#-----------------------------------------------------------------------------------
import sublime
import sublime_plugin
import os
import pickle
import re

ST3 = int(sublime.version()) > 3000

if ST3:
    import UnrealScriptIDE.UnrealScriptIDEData as USData
    import UnrealScriptIDE.UnrealScriptIDEParser as Parser
else:
    import UnrealScriptIDEData as USData
    import UnrealScriptIDEParser as Parser


# get the event manager
def evt_m():
    return event_manager

event_manager = None


def is_unrealscript_file():
    window = sublime.active_window()
    if window:
        view = window.active_view()
        if view:
            return "UnrealScriptIDE/UnrealScript.tmLanguage" in view.settings().get('syntax')
    return False


# returns the code fragment that is actually relevant for auto-completion / go to declaration.
# e.g. a single statement
#   something = other + function(a, b).foo. returns function().foo.
#   # I have no idea how this works, sorry.
def get_relevant_text(text):
    left_line = text.lstrip().lower()
    i = 0
    obj_string = ""
    for c in left_line[::-1]:
        obj_string += c
        if c == ')':
            i += 1
        elif c == '(':
            i -= 1
        if (c == ' ' or c == ',' or c == '\t') and i == 0:
            return get_rid_of_arguments(obj_string[-2::-1])
        elif c == '(' and i == -1:
            return get_rid_of_arguments(obj_string[-2::-1])
    obj_string = obj_string[::-1].lstrip()
    return get_rid_of_arguments(obj_string)


# gets rid of function arguments: foo(a.ssdd, asd.eef.sd()).go(sd, ds()) -> foo().go()
def get_rid_of_arguments(text):
    i, start, end = 0, 0, 0
    result = ""
    for j, c in enumerate(text):
        if c == '(':
            if i == 0:
                start = j
                result += text[end:start]
            i += 1
        elif c == ')':
            i -= 1
            if i == 0:
                end = j + 1
                result += '()'
    if result == "":
        return text
    return result + text[end:]


####################################################
# Auto Completion and Go to declaration
# ---------------------------
#
####################################################


# Creates threads (ParserThread) for collecting any function, event or variable
# Handles all events
# Also, this is the main instance of my plug-in.
class UnrealScriptIDEMain(USData.UnrealData, sublime_plugin.EventListener):
    # at startup, this is true
    b_first_time = True
    # when the first UnrealScript file is opened, all classes will be parsed.
    # During this time, no other threads shall be created and this will be True.
    b_still_parsing_classes = True

    # active threads
    _collector_threads = []
    # will be true when the parsing happened to parse the current file.
    b_built_for_current_file = False
    # will be set to true just after auto-completion
    b_did_autocomplete = False
    # if it needs to parse first before going to the declaration, this will be true.
    b_wanted_to_autocomplete = False
    # if it needs to parse before go to definition, this is true.
    b_wanted_to_go_to_definition = False

    # the line number at which the help panel was displayed last
    help_panel_line_number = -1

    # the path to the src folder. This is used to save and load the cache files.
    src_folder = ""

    # if true, the parser will rebuild all files.
    b_rebuild_cache = False

    # ! (TODO): clear completions for current file
    # def on_close(self, view):
    #     pass
    # gets called when a file is saved. re-parse the current file.
    def on_post_save(self, view, _async=False):
        if ST3 and not _async:
            return
        if is_unrealscript_file():
            filename = view.file_name()
            if filename:
                self.remove_file(filename)
                if ST3:
                    self.on_activated_async(view)
                else:
                    self.on_activated(view)

    def on_post_save_async(self, view):
        self.on_post_save(view, _async=True)

    # start parsing the active file when a tab becomes active
    # at first startup, scan for all classes and save them to _classes
    # at later startups, load _classes from cache.
    def on_activated(self, view, _async=False):
        if ST3 and not _async:
            return
        if is_unrealscript_file():
            self.clear()    # empty the completions list, so that we only get the relevant ones.
            self.b_built_for_current_file = True

            window = view.window()
            # load breakpoints
            if window:
                window.run_command("unreal_load_breakpoints")

            # at startup, save all classes
            if self.b_first_time:
                self.b_first_time = False
                # register events
                global event_manager
                event_manager = EventManager()
                evt_m().go_to_definition += self.on_go_to_definition
                evt_m().rebuild_cache += self.on_rebuild_cache
                evt_m().get_class_reference += self.on_get_classes_reference
                evt_m().get_and_open_object += self.get_and_open_object

                view.set_status('UnrealScriptAutocomplete', "startup: start parsing classes...")
                print("startup: start parsing classes...")
                open_folder_arr = window.folders()   # Gets all opened folders in the Sublime Text editor.
                self._collector_threads.append(Parser.ClassesCollectorThread(self, "", 30, open_folder_arr, True))
                self._collector_threads[-1].start()
                self.handle_threads(self._collector_threads, view)  # display progress bar
                return

            file_name = view.file_name()
            # wait for the classes threads to be completed, then parse the current file.
            if not self.b_still_parsing_classes and file_name is not None:
                # if the file wasn't parsed before, parse it now.
                if file_name not in self._filenames:
                    print("start parsing file: ", file_name)
                    self._filenames.append(file_name)
                    self.add_function_collector_thread(file_name)  # create a new thread to search for relevant functions for the active file
                    self.handle_threads(self._collector_threads, view)  # display progress bar

                else:
                    print("already parsed, load completions for file: ", file_name)
                    self.load_completions_for_file(file_name)

    def on_activated_async(self, view):
        self.on_activated(view,True)

    # This function is called when auto-complete pop-up box is displayed.
    # Used to get context sensitive suggestions
    def on_query_completions(self, view, prefix, locations, _async=False):
        if is_unrealscript_file():
            selection_region = view.sel()[0]
            line = view.line(selection_region)
            left_line_region = sublime.Region(line.begin(), selection_region.end())

            line_contents = view.substr(left_line_region)
            split_lines = line_contents.split()

            # if on a class declaration line
            if len(split_lines) >= 1 and "class" == split_lines[0].lower():
                # only get classes:
                if len(split_lines) >= 3 and "extends" == split_lines[2].lower():
                    return self.get_autocomplete_list(prefix, False, True, True, bNoStandardCompletions=True)
                # only keywords
                return [(item + "\tkeyword", item) for item in self.get_keywords()]

            # if is in defaultproperties, only get variables:
            line_number = 1000000
            defaultproperties_region = view.find('defaultproperties', 0, sublime.IGNORECASE)
            if defaultproperties_region:
                # if "MyAsset = ", and MyAsset is an asset type (Texture2D, SoundCue, ...)
                # set b_no_assets=False
                b_no_assets = True
                assets_filtering = None
                match = re.match(r"(\w+)[ \t]*=", line_contents.strip().lower())
                if match:
                    var = match.group(1)
                    o = self.get_object(var, self)
                    if o:
                        type_ = o.type()
                        if type_:
                            class_ = self.get_object(type_, self, b_no_functions=True, b_no_variables=True)
                            if class_:
                                assets_filtering = class_.all_child_classes()
                            else:
                                assets_filtering = [type_]
                            b_no_assets = False

                line_number, col = view.rowcol(defaultproperties_region.a)
                row, col = view.rowcol(selection_region.begin())
                if row > line_number:
                    # below defaultproperties
                    # if declaring begin object block
                    if line_contents.strip().lower() == "begin object class=":
                        return self.get_autocomplete_list(prefix, b_no_functions=True, b_no_variables=True, bNoStandardCompletions=True)

                    # if inside a begin object block, get object oriented completions
                    # find_all(pattern, <flags>, <format>, <extractions>)
                    result = []
                    begin_objects = view.find_all(r"begin\sobject\sclass\s?=\s?(\w+)\sname\s?=\s?\w+|begin\sobject\sname\s?=\s?\w+\sclass\s?=\s?(\w+)|begin\sobject\sname\s?=\s?\w+", sublime.IGNORECASE, "\\1\\2", result)
                    end_objects = view.find_all(r"end object", sublime.IGNORECASE)
                    if begin_objects and end_objects:
                        regions = zip([view.rowcol(r.a)[0] for r in begin_objects], [view.rowcol(r.a)[0] for r in end_objects])
                        for i, p in enumerate(regions):
                            if p[0] < row < p[1]:
                                print("in region: ", p, result[i])
                                c = self.get_class(result[i])
                                if not c:
                                    c = "type not found"
                                    print("nothing found for: ", result[i])
                                    break
                                if c.has_parsed():
                                    return self.get_autocomplete_list(prefix, True, True, False, c, bNoStandardCompletions=True, b_no_assets=b_no_assets, assets_filtering=assets_filtering)
                                else:
                                    c.parse_me()
                                    self.b_wanted_to_autocomplete = True
                                    return [("just a moment...", ""), ("", "")]

                    return self.get_autocomplete_list(prefix, True, True, bNoStandardCompletions=True, b_no_assets=b_no_assets, assets_filtering=assets_filtering)

            # no defaultproperties found or above defaults:

            # on a variable declaration line:
            if len(split_lines) > 0 and (any([x == split_lines[0].lower() for x in ["var", "local", "param"]]) or "var(" in split_lines[0].lower()):
                # not an array
                if len(split_lines) > 1 and not "array" in split_lines[1].lower():
                    if any(line_contents[-1] == c for c in ["<", "|"]):
                        return [(item + "\tmetadata tag", item) for item in self.get_metadata_tags()]

            # check if inside a function, get parameters and local variables:
            local_vars = []
            super_txt = ""
            if len(split_lines) > 0:
                region = sublime.Region(0, locations[0])
                contents = view.substr(region)
                # reverse by lines:
                rev_content = reversed(contents.split('\n'))
                f_reg = re.compile(r"([a-zA-Z0-9()\s]*?)function[\s]+((coerce)\s*)?([a-zA-z0-9<>_]*?)[\s]*([a-zA-z0-9_-]+)([\s]*\(+)(.*)((\s*\))+)[\s]*(const)?[\s]*;?[\s]*(\/\/.*)?")
                e_reg = re.compile(r"([a-zA-Z0-9()\s]*?)event[\s]+((coerce)\s*)?([a-zA-z0-9<>_]*?)[\s]*([a-zA-z0-9_-]+)([\s]*\(+)(.*)((\s*\))+)[\s]*(const)?[\s]*;?[\s]*(\/\/.*)?")
                line_number = view.rowcol(locations[0])[0]
                for line in rev_content:
                    # Yes I know this is bad, actually it's very bad. But I don't want to refract the parser again.
                    # ########
                    if "local" in line:  # get possible lines containing variables
                        # 1: vartype, 2: name, 3: documentation
                        var_doc_line = line.split('//')
                        if len(var_doc_line) < 2:
                            var_doc_line = line.split('/**')
                        var_line = var_doc_line[0].split()
                        if var_line and "local" not in var_line[0]:
                            continue
                        elif not var_line:
                            continue

                        doc_line = ''
                        if len(var_doc_line) > 1:
                            doc_line = var_doc_line[1].rstrip()

                        var_names = []
                        var_names.append(var_line.pop().rstrip('\n\r\t ;'))     # get the right most variable
                        for v in var_line:
                            if "," in var_line[-1]:     # if there are multiple variable names in one line separated by ',' , get them.
                                var_names.append(var_line.pop().rstrip('\n\r\t ,'))
                            else:
                                break
                        for name in var_names:
                            if "<" in name or ">" in name:
                                name = re.sub(r'\<.*?\>', '', name)
                            local_vars.append(USData.Variable(var_line, name, doc_line, line_number, ""))
                    # ########
                    line_number -= 1

                    match_f, match_e = f_reg.match(line), e_reg.match(line)

                    if match_f or match_e:
                        match = match_f if match_f else match_e
                        if match.group(5):
                            try:
                                super_txt = "super." + match.group(5) + "(" + ", ".join([x.split()[-1] for x in match.group(7).split(',')]) + ")"
                            except IndexError:
                                super_txt = "super." + match.group(5) + "()"
                        if match.group(7):
                            for param in match.group(7).split(','):
                                local_vars.append(USData.Variable(["param"] + param.strip().split()[:-1], param.split()[-1], "blajsn", line_number, ""))
                                print(local_vars[-1].name(), local_vars[-1].var_modifiers())
                        break

            # check if wants object oriented completions
            if len(line_contents) > 0 and line_contents[-1] == '.':
                left_line = get_relevant_text(line_contents)
                if '.' != left_line[-1]:
                    left_line = ".".join(left_line.split('.')[:-1]) + '.'
                # print("object.* :  ", left_line)

                c = self.get_class_from_context(left_line, local_vars=local_vars)
                if not c:
                    c = "type not found"
                    print("nothing found for: ", left_line)
                if c != "parsing...":
                    return self.get_autocomplete_list(prefix, True, False, False, c, bNoStandardCompletions=True)
                else:
                    self.b_wanted_to_autocomplete = True
                    return [("just a moment...", ""), ("", "")]

            # get standard completions
            else:
                compl_default = [view.extract_completions(prefix)]
                compl_default = [(item + "\tbuffer", item) for sublist in compl_default for item in sublist]       # format
                keywords = [(item + "\tkeyword", item) for item in self.get_keywords()]
                return self.get_autocomplete_list(prefix, local_vars=local_vars) + keywords + compl_default + [(super_txt, super_txt)]

    # called right before auto completion.
    def on_query_context(self, view, key, operator, operand, match_all):
        if is_unrealscript_file():
            if key == "insert_dynamic_snippet":
                region = view.sel()[0]
                if region.empty():
                    self.b_did_autocomplete = True

    # remove auto completion and insert dynamic snippet instead, just after auto completion
    def on_modified(self, view, _async=False):
        if ST3 and not _async:
            return
        if is_unrealscript_file():
            # if the helper panel has just been displayed, save the line number
            if USData.b_helper_panel_on:
                self.help_panel_line_number = view.rowcol(view.sel()[0].begin())[0]
                USData.b_helper_panel_on = False

            elif self.help_panel_line_number != -1:
                # if we are modifying anything above or below the helper panel line, hide the panel.
                lno = view.rowcol(view.sel()[0].begin())[0]
                if lno != self.help_panel_line_number and lno != 0:
                    view.window().run_command("hide_panel", {"panel": "output.UnrealScriptAutocomplete_panel"})
                    self.help_panel_line_number = -1

            if self.b_did_autocomplete:
                self.b_did_autocomplete = False
                # use timeout to reduce time needed inside the on_modified event
                sublime.set_timeout(lambda: self.insert_dynamic_snippet_for_completion(view, self.completion_class), 0)

    def on_modified_async(self, view):
        self.on_modified(view, _async=True)

    # if there is a dynamic snippet available for the just added word,
    # remove the last word and insert the snippet instead
    # if from_class is given, find the object inside this class
    def insert_dynamic_snippet_for_completion(self, view, from_class=None):
        region_word = view.word(view.sel()[0])
        word = view.substr(region_word)

        if not all(c.isspace() for c in word):  # only if the current word doesn't contain any whitespace character
            if from_class:
                o = self.get_object(word, from_class)
                self.completion_class = None
            else:
                o = self.get_object(word, self)

            if o:
                if ST3:
                    view.window().run_command('replace_region', {'regionA': region_word.a, 'regionB': region_word.b})
                else:
                    edit = view.begin_edit('UnrealScriptAutocomplete')
                    view.replace(edit, region_word, "")     # remove last word
                    view.end_edit(edit)
                o.insert_dynamic_snippet(view)

    # go to the definition of the object below the cursor
    def on_go_to_definition(self, left_line, word, full_line, b_new_start_point):
        window = sublime.active_window()
        # print("on_go_to_definition: full_line:\t", full_line, "\t left_line:\t'" + left_line + "'\t Word:\t", word)

        # probably a declaration or super.
        # => go to the parent declaration
        if "function" in full_line or "event" in full_line or left_line[-6:] == "super.":
            # try opening a class, if it fails, its a declaration (or super.)
            #                      if it doesn't, it's the return type
            if not self.get_and_open_object(word, self, window, b_new_start_point, False, True, True):
                # open parent declaration
                active_file = window.active_view().file_name()
                c = self.get_class_from_filename(active_file).get_parent()
                # c = self.get_class(self.get_class_from_filename(active_file).parent_class())
                self.get_and_open_object(word, c, window, b_new_start_point, True)

        # just a single object or self.
        elif left_line == "" or left_line[-5:] == "self.":
            if word == "super":
                # open parent class
                active_file = window.active_view().file_name()
                c = self.get_class_from_filename(active_file).parent_class()
                self.get_and_open_object(c, self, window, b_new_start_point)
            elif word == "self":
                # open the the declaration of the current file
                active_file = window.active_view().file_name()
                self.get_and_open_object(self.get_class_from_filename(active_file).name(), self, window, b_new_start_point)
            else:
                # open the declaration of the object
                self.get_and_open_object(word, self, window, b_new_start_point)

        # a dot before the object
        elif left_line != "" and left_line[-1] == '.':
            c = self.get_class_from_context(left_line)
            if c == "parsing...":
                window.active_view().set_status('UnrealScriptAutocomplete', "just a moment...")
                print("still parsing...")
                self.b_wanted_to_go_to_definition = True
                self.b_new_start_point = b_new_start_point
            else:
                self.get_and_open_object(word, c, window, b_new_start_point, True)
        else:
            print("case not handled!!!", left_line)

    # gets the object out of out_of and if found opens it
    # ! TODO: if there is a variable and a class, ask which to open.
    def get_and_open_object(self, word, out_of, window, b_new_start_point, b_no_classes=False, b_no_functions=False, b_no_variables=False):
        o = self.get_object(word, out_of, b_no_classes, b_no_functions, b_no_variables)
        # print("object ", o)
        if o is not None and o != "parsing...":
            window.run_command("unreal_goto_definition", {"b_new_start_point": b_new_start_point, "line_number": o.line_number(), "filename": o.file_name()})
            return True
        elif o == "parsing...":
            window.active_view().set_status('UnrealScriptAutocomplete', "just a moment...")
            self.b_wanted_to_go_to_definition = True
            self.b_new_start_point = b_new_start_point
        else:
            window.active_view().set_status('UnrealScriptAutocomplete', word + " not found in current file and all parent classes!")
        return False

    # creates a thread to parse the given file_name and all its parent classes
    def add_function_collector_thread(self, file_name):
        self._collector_threads.append(Parser.ParserThread(self, file_name, 30))
        self._collector_threads[-1].start()

    # animates an activity bar.
    # serves as an event for when all threads are done
    def handle_threads(self, threads, view, i=0, dir=1):
        # remove finished threads
        for thread in threads:
            if not thread.isAlive():
                threads.remove(thread)

        if len(threads):
            # This animates a little activity indicator in the status area
            before = i % 8
            after = (7) - before
            if not after:
                dir = -1
            if not before:
                dir = 1
            i += dir
            view.set_status('UnrealScriptAutocomplete', 'UnrealScriptAutocomplete is Parsing [%s=%s]' % (' ' * before, ' ' * after))

            sublime.set_timeout(lambda: self.handle_threads(threads, view, i, dir), 100)
            return
        else:
            self.save_classes_to_cache()
            view.erase_status('UnrealScriptAutocomplete')
            if self.b_still_parsing_classes:
                print("finished parsing classes, start parsing current file")
                self.b_still_parsing_classes = False
                # self.save_classes_to_cache()
                self.link_classes()
                if ST3:
                    self.on_activated_async(view)
                else:
                    self.on_activated(view)
            else:
                if self.b_wanted_to_go_to_definition:
                    print("wanted to go to definition!")
                    self.b_wanted_to_go_to_definition = False
                    sublime.active_window().run_command("unreal_goto_definition", {"b_new_start_point": self.b_new_start_point})
                elif self.b_wanted_to_autocomplete:
                    print("wanted to auto-complete!")
                    self.b_wanted_to_autocomplete = False
                    sublime.active_window().run_command("hide_auto_complete")
                    sublime.set_timeout(lambda: view.run_command("auto_complete"), 0)
                else:
                    # finished and keep functions for later use
                    if self.b_built_for_current_file:
                        self.b_built_for_current_file = False
                        self._functions, self._variables = self.get_completions_from_class(view.file_name())
                        self.save_completions_to_file(view.file_name())
                    evt_m().parsing_finished()

    # reset all and start from anew
    def clear_all(self, view):
        self.b_first_time = True
        self.b_rebuild_cache = True
        self.clear()
        self._completions_for_file = []
        self._filenames = []
        for c in self._classes:
            c.clear()
        self._classes = []
        self.b_still_parsing_classes = True
        if ST3:
            self.on_activated_async(view)
        else:
            self.on_activated(view)

    # save the _classes array to a cache file in the src folder
    def save_classes_to_cache(self):
        if os.path.exists(self.src_folder):
            if ST3:
                with open(os.path.join(self.src_folder, 'classes_cache.obj'), 'wb') as cache_file:
                    pickle.dump(self._classes, cache_file)
            else:
                with open(os.path.join(self.src_folder, 'classes_cache.obj'), 'w') as cache_file:
                    pickle.dump(self._classes, cache_file)

    # loads the _classes from the cache file
    def load_classes_from_cache(self):
        if os.path.exists(self.src_folder):
            if ST3:
                with open(os.path.join(self.src_folder, 'classes_cache.obj'), 'rb') as cache_file:
                    self._classes = pickle.load(cache_file)
            else:
                with open(os.path.join(self.src_folder, 'classes_cache.obj'), 'r') as cache_file:
                    self._classes = pickle.load(cache_file)
            for c in self._classes:
                c.set_collector_reference(self)

    def on_rebuild_cache(self, view):
        print("rebuild cache")
        self.clear_all(view)

    def on_get_classes_reference(self, callback):
        callback(self.get_object("Object", self, b_no_functions=True, b_no_variables=True))

    def get_keywords(self):
        settings = sublime.load_settings('UnrealScriptIDE.sublime-settings')
        return settings.get('unreal_keywords')

    def get_metadata_tags(self):
        settings = sublime.load_settings('UnrealScriptIDE.sublime-settings')
        return settings.get('metadata_tags')


# this deletes the cache file and clears every completion, so that it can then rebuild the classes.
# Resetting everything, basically starting from anew like it would be the first run.
class UnrealRebuildCacheCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if is_unrealscript_file():
            open_folder_arr = self.view.window().folders()
            if open_folder_arr:
                for f in open_folder_arr:
                    if "Development\\Src" in f:
                        # if we saved the classes to a cache before, delete it.
                        if os.path.exists(os.path.join(f, "classes_cache.obj")):
                            evt_m().rebuild_cache(self.view)
        else:
            print("no UnrealScript file, try again with a .uc file focused")


########################################################
#Event
#-----
# this one is taken from: http://www.valuedlessons.com/2008/04/events-in-python.html
########################################################
class Event:
    def __init__(self):
        self.handlers = set()

    def handle(self, handler):
        self.handlers.add(handler)
        return self

    def unhandle(self, handler):
        try:
            self.handlers.remove(handler)
        except:
            raise ValueError("Handler is not handling this event, so cannot unhandle it.")
        return self

    def fire(self, *args, **kargs):
        for handler in self.handlers:
            handler(*args, **kargs)

    def getHandlerCount(self):
        return len(self.handlers)

    __iadd__ = handle
    __isub__ = unhandle
    __call__ = fire
    __len__ = getHandlerCount


class EventManager():
    def __init__(self):
        self.parsing_finished = Event()
        self.go_to_definition = Event()
        self.rebuild_cache = Event()
        self.get_class_reference = Event()
        self.get_and_open_object = Event()


class ReplaceRegionCommand(sublime_plugin.TextCommand):
    def run(self, edit, regionA, regionB, text=""):
        region = sublime.Region(regionA, regionB)
        self.view.replace(edit, region, text)

# def on_parsing_finished(self, arg):
#     print(arg)

# evt_m().parsing_finished += self.on_parsing_finished
