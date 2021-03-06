# -*- coding: utf-8 -*-
#
# This file is part of NINJA-IDE (http://ninja-ide.org).
#
# NINJA-IDE is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# any later version.
#
# NINJA-IDE is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with NINJA-IDE; If not, see <http://www.gnu.org/licenses/>.
from __future__ import absolute_import
from __future__ import unicode_literals

import re
import sys

from tokenize import generate_tokens, TokenError
import token as tkn
#lint:disable
try:
    from StringIO import StringIO
except:
    from io import StringIO
#lint:enable

from PyQt4.QtGui import QPlainTextEdit
from PyQt4.QtGui import QFontMetricsF
from PyQt4.QtGui import QToolTip
from PyQt4.QtGui import QAction
from PyQt4.QtGui import QTextOption
from PyQt4.QtGui import QTextEdit
from PyQt4.QtGui import QInputDialog
from PyQt4.QtGui import QTextCursor
from PyQt4.QtGui import QTextDocument
from PyQt4.QtGui import QTextFormat
from PyQt4.QtGui import QFont
from PyQt4.QtGui import QMenu
from PyQt4.QtGui import QPainter
from PyQt4.QtGui import QColor
from PyQt4.QtCore import QRect
from PyQt4.QtCore import SIGNAL
from PyQt4.QtCore import QMimeData
from PyQt4.QtCore import Qt

from ninja_ide import resources
from ninja_ide.core import settings
from ninja_ide.core.file_handling import file_manager
from ninja_ide.tools.completion import completer_widget
from ninja_ide.gui.ide import IDE
from ninja_ide.gui.editor import highlighter
from ninja_ide.gui.editor import syntax_highlighter
from ninja_ide.gui.editor import helpers
from ninja_ide.gui.editor import minimap
from ninja_ide.gui.editor import sidebar_widget
from ninja_ide.gui.editor import python_syntax

from ninja_ide.tools.logger import NinjaLogger

BRACE_DICT = {')': '(', ']': '[', '}': '{', '(': ')', '[': ']', '{': '}'}
logger = NinjaLogger('ninja_ide.gui.editor.editor')


if sys.version_info.major == 3:
    python3 = True
else:
    python3 = False


class Editor(QPlainTextEdit):

###############################################################################
# EDITOR SIGNALS
###############################################################################
    """
    modificationChanged(bool)
    fileSaved(QPlainTextEdit)
    locateFunction(QString, QString, bool) [functionName, filePath, isVariable]
    openDropFile(QString)
    addBackItemNavigation()
    checksFound(QPlainTextEdit, PyQt_PyObject)
    cleanDocument(QPlainTextEdit)
    findOcurrences(QString)
    cursorPositionChange(int, int)    #row, col
    migrationAnalyzed()
    currentLineChanged(int)
    """
###############################################################################

    def __init__(self, neditable):
        super(Editor, self).__init__()
        self._neditable = neditable
        #Config Editor
        self.set_flags()
        self.__lines_count = None
        self.lang = 'python'
        self._last_block_position = 0
        self.__lines_count = 0

        self._sidebarWidget = sidebar_widget.SidebarWidget(self, neditable)

        self.highlighter = None
        self.allows_less_indentation = ['else', 'elif', 'finally', 'except']
        #Set editor style
        self.apply_editor_style()
        self.set_font(settings.FONT_FAMILY, settings.FONT_SIZE)
        #For Highlighting in document
        self.extraSelections = []
        self._selected_word = ''
        self._patIsWord = re.compile('\w+')
        #Brace matching
        self._braces = None
        self.__encoding = None
        #Completer
        self.completer = completer_widget.CodeCompletionWidget(self)
        #Flag to dont bug the user when answer *the modification dialog*
        #Dict functions for KeyPress
        self.preKeyPress = {
            Qt.Key_Tab: self.__insert_indentation,
            Qt.Key_Backspace: self.__backspace,
            Qt.Key_Home: self.__home_pressed,
            Qt.Key_Enter: self.__ignore_extended_line,
            Qt.Key_Return: self.__ignore_extended_line,
            Qt.Key_Colon: self.__retreat_to_keywords,
            Qt.Key_BracketRight: self.__brace_completion,
            Qt.Key_BraceRight: self.__brace_completion,
            Qt.Key_ParenRight: self.__brace_completion,
            Qt.Key_Apostrophe: self.__quot_completion,
            Qt.Key_QuoteDbl: self.__quot_completion}

        self.postKeyPress = {
            Qt.Key_Enter: self.__auto_indent,
            Qt.Key_Return: self.__auto_indent,
            Qt.Key_BracketLeft: self.__complete_braces,
            Qt.Key_BraceLeft: self.__complete_braces,
            Qt.Key_ParenLeft: self.__complete_braces,
            Qt.Key_Apostrophe: self.__complete_quotes,
            Qt.Key_QuoteDbl: self.__complete_quotes}

        self._current_line_color = QColor(
            resources.CUSTOM_SCHEME.get('current-line',
            resources.COLOR_SCHEME['current-line']))

        self.connect(self, SIGNAL("updateRequest(const QRect&, int)"),
            self._sidebarWidget.update_area)
        #FIXME: Should file saved be handled by neditable??
        self.connect(self, SIGNAL("undoAvailable(bool)"), self._file_saved)
        self.connect(self, SIGNAL("cursorPositionChanged()"),
            self.highlight_current_line)
        self.connect(self, SIGNAL("blockCountChanged(int)"),
            self._update_file_metadata)
        self.connect(self.document(), SIGNAL("modificationChanged(bool)"),
            lambda x: self.emit(SIGNAL("modificationChanged(bool)"), x))

        self._mini = None
        if settings.SHOW_MINIMAP:
            self._mini = minimap.MiniMap(self)
            self._mini.show()

        #Indentation
        self.load_project_config()
        #Context Menu Options
        self.__actionFindOccurrences = QAction(
            self.tr("Find Usages"), self)
        self.connect(self.__actionFindOccurrences, SIGNAL("triggered()"),
            self._find_occurrences)

        # Set the editor after initialization
        self._neditable.set_editor(self)

        if self._mini:
            self._mini.set_code(self.toPlainText())

        ninjaide = IDE.get_service('ide')
        self.connect(ninjaide,
            SIGNAL("ns_preferences_editor_fontFamily(PyQt_PyObject)"),
            lambda: self.set_font())

    @property
    def display_name(self):
        self._neditable.display_name

    @property
    def nfile(self):
        return self._neditable.nfile

    @property
    def neditable(self):
        return self._neditable

    @property
    def file_path(self):
        return self._neditable.file_path

    @property
    def is_modified(self):
        return self.document().isModified()

    def load_project_config(self):
        ninjaide = IDE.get_service('ide')
        project = ninjaide.get_project_for_file(self._neditable.file_path)
        if project is not None:
            self.indent = project.indentation
            self.useTabs = project.use_tabs
            #Set tab usage
            if self.useTabs:
                self.set_tab_usage()
            self.connect(project, SIGNAL("projectPropertiesUpdated()"),
                self.load_project_config)
            self.additional_builtins = project.additional_builtins
        else:
            self.indent = settings.INDENT
            self.useTabs = settings.USE_TABS
            self.additional_builtins = None

    def __retreat_to_keywords(self, event):
        """Unindent some kind of blocks if needed."""
        previous_text = unicode(self.textCursor().block().previous().text())
        current_text = unicode(self.textCursor().block().text())
        previous_spaces = helpers.get_indentation(previous_text)
        current_spaces = helpers.get_indentation(current_text)

        if len(previous_spaces) < len(current_spaces):
            last_word = helpers.get_first_keyword(current_text)

            if last_word in self.allows_less_indentation:
                helpers.clean_line(self)

                spaces_diff = len(current_spaces) - len(previous_spaces)
                self.textCursor().insertText(current_text[spaces_diff:])

    def __get_encoding(self):
        """Get the current encoding of 'utf-8' otherwise."""
        if self.__encoding is not None:
            return self.__encoding
        return 'utf-8'

    def __set_encoding(self, encoding):
        """Set the current encoding."""
        self.__encoding = encoding

    encoding = property(__get_encoding, __set_encoding)

    def set_flags(self):
        """Set some configuration flags for the Editor."""
        if settings.ALLOW_WORD_WRAP:
            self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        else:
            self.setWordWrapMode(QTextOption.NoWrap)
        self.setMouseTracking(True)
        doc = self.document()
        option = QTextOption()
        if settings.SHOW_TABS_AND_SPACES:
            option.setFlags(QTextOption.ShowTabsAndSpaces)
        doc.setDefaultTextOption(option)
        self.setDocument(doc)
        self.setCenterOnScroll(settings.CENTER_ON_SCROLL)

    def set_tab_usage(self):
        """Update tab stop width and margin line."""
        tab_size = self.pos_margin / settings.MARGIN_LINE * self.indent
        self.setTabStopWidth(tab_size)
        if self._mini:
            self._mini.setTabStopWidth(tab_size)

    def _block_contains_text(self):
        block = self.textCursor().block()
        return len(block.text().strip()) != 0

    def _update_file_metadata(self, val):
        """Update the info of bookmarks, breakpoint and checkers."""
        if (self._sidebarWidget.bookmarks or
           self._sidebarWidget.breakpoints or
           self._sidebarWidget.foldedBlocks):
            diference = val - self.__lines_count
            cursor = self.textCursor()
            blockNumber = cursor.blockNumber() - abs(diference)
            self._sidebarWidget.update_sidebar_marks(blockNumber, diference,
                self._block_contains_text())
        if self._neditable.has_checkers:
            diference = val - self.__lines_count
            cursor = self.textCursor()
            blockNumber = cursor.blockNumber() - abs(diference)
            self._neditable.update_checkers_metadata(blockNumber, diference,
                self._block_contains_text())
        self.__lines_count = val
        self.highlight_current_line()

    def _show_tab_icon_notification(self):
        signal_emitted = False
        for items in self._neditable.registered_checkers:
            checker, _, _ = items
            if checker.checks:
                self.emit(
                    SIGNAL("checksFound(QPlainTextEdit, PyQt_PyObject)"),
                    self, checker.checker_icon)
                signal_emitted = True

        if not signal_emitted:
            self.emit(SIGNAL("cleanDocument(QPlainTextEdit)"), self)
        #TODO:
        #if self.highlighter:
            #lines = list(set(list(self.errors.errorsSummary.keys()) +
                        #list(self.pep8.pep8checks.keys())))
            #self.highlighter.rehighlight_lines(lines)
            #self.highlight_current_line()

    def restyle(self, syntaxLang=None):
        self.apply_editor_style()
        if self.lang == 'python':
            parts_scanner, code_scanner, formats = \
                syntax_highlighter.load_syntax(python_syntax.syntax)
            self.highlighter = syntax_highlighter.SyntaxHighlighter(
                self.document(),
                parts_scanner, code_scanner, formats, self._neditable)
            if self._mini:
                self._mini.highlighter = syntax_highlighter.SyntaxHighlighter(
                    self._mini.document(), parts_scanner,
                    code_scanner, formats)
            return
        if self.highlighter is None or isinstance(self.highlighter,
           highlighter.EmpyHighlighter):
            self.highlighter = highlighter.Highlighter(self.document(),
                None, resources.CUSTOM_SCHEME, self.errors, self.pep8,
                self.migration)
        if not syntaxLang:
            ext = file_manager.get_file_extension(self.file_path)
            self.highlighter.apply_highlight(
                settings.EXTENSIONS.get(ext, 'python'),
                resources.CUSTOM_SCHEME)
            if self._mini:
                self._mini.highlighter.apply_highlight(
                    settings.EXTENSIONS.get(ext, 'python'),
                    resources.CUSTOM_SCHEME)
        else:
            self.highlighter.apply_highlight(
                syntaxLang, resources.CUSTOM_SCHEME)
            if self._mini:
                self._mini.highlighter.apply_highlight(
                    syntaxLang, resources.CUSTOM_SCHEME)

    def apply_editor_style(self):
        css = 'QPlainTextEdit {color: %s; background-color: %s;' \
            'selection-color: %s; selection-background-color: %s;}' \
            % (resources.CUSTOM_SCHEME.get('editor-text',
            resources.COLOR_SCHEME['editor-text']),
            resources.CUSTOM_SCHEME.get('editor-background',
                resources.COLOR_SCHEME['editor-background']),
            resources.CUSTOM_SCHEME.get('editor-selection-color',
                resources.COLOR_SCHEME['editor-selection-color']),
            resources.CUSTOM_SCHEME.get('editor-selection-background',
                resources.COLOR_SCHEME['editor-selection-background']))
        self.setStyleSheet(css)

    def _file_saved(self, undoAvailable=False):
        if not undoAvailable:
            self.emit(SIGNAL("fileSaved(QPlainTextEdit)"), self)
            self.document().setModified(False)
            if self._mini:
                self._mini.set_code(self.toPlainText())

    def register_syntax(self, lang='', syntax=None):
        #self.lang = settings.EXTENSIONS.get(lang, 'python')
        #if self.lang == 'python':
        parts_scanner, code_scanner, formats = \
            syntax_highlighter.load_syntax(python_syntax.syntax)
        self.highlighter = syntax_highlighter.SyntaxHighlighter(
            self.document(),
            parts_scanner, code_scanner, formats, self._neditable)
        if self._mini:
            self._mini.highlighter = syntax_highlighter.SyntaxHighlighter(
                self._mini.document(), parts_scanner,
                code_scanner, formats, self._neditable)
        #elif lang in settings.EXTENSIONS:
            #self.highlighter = highlighter.Highlighter(self.document(),
                #self.lang, resources.CUSTOM_SCHEME, self.errors, self.pep8,
                #self.migration)
            #if self._mini:
                #self._mini.highlighter = highlighter.Highlighter(
                    #self._mini.document(),
                    #self.lang, resources.CUSTOM_SCHEME)
        #elif syntax is not None:
            #self.highlighter = highlighter.Highlighter(self.document(),
                #None, resources.CUSTOM_SCHEME)
            #self.highlighter.apply_highlight(lang, resources.CUSTOM_SCHEME,
                #syntax)
            #if self._mini:
                #self._mini.highlighter = highlighter.Highlighter(
                    #self.document(), None, resources.CUSTOM_SCHEME)
                #self._mini.highlighter.apply_highlight(lang,
                    #resources.CUSTOM_SCHEME, syntax)
        #else:
            #self.highlighter = highlighter.EmpyHighlighter(self.document())
            #if self._mini:
                #self._mini.highlighter = highlighter.EmpyHighlighter(
                    #self.document())

    def get_text(self):
        """
        Returns all the plain text of the editor
        """
        return self.toPlainText()

    def get_lines_count(self):
        """
        Returns the count of lines in the editor
        """
        return self.textCursor().document().lineCount()

    def cursor_inside_string(self):
        inside = False
        cursor = self.textCursor()
        pos = cursor.positionInBlock()
        user_data = syntax_highlighter.get_user_data(cursor.block())
        for vals in user_data.str_groups:
            if vals[0] < pos < vals[1]:
                inside = True
                break
        return inside

    def cursor_inside_comment(self):
        inside = False
        cursor = self.textCursor()
        pos = cursor.positionInBlock()
        user_data = syntax_highlighter.get_user_data(cursor.block())
        if (user_data.comment_start != -1) and \
           (pos > user_data.comment_start):
            inside = True
        return inside

    def set_font(self, family=None, size=None):
        if family is None:
            family = settings.FONT_FAMILY
        if size is None:
            size = settings.FONT_SIZE
        font = QFont(family, size)
        self.document().setDefaultFont(font)
        self._update_margin_line(font)

    def jump_to_line(self, lineno=None):
        """
        Jump to a specific line number or ask to the user for the line
        """
        if lineno is not None:
            self.emit(SIGNAL("addBackItemNavigation()"))
            self.go_to_line(lineno)
            return

        maximum = self.blockCount()
        line = QInputDialog.getInt(self, self.tr("Jump to Line"),
            self.tr("Line:"), 1, 1, maximum, 1)
        if line[1]:
            self.emit(SIGNAL("addBackItemNavigation()"))
            self.go_to_line(line[0] - 1)

    def _find_occurrences(self):
        if self.textCursor().hasSelection():
            word = self.textCursor().selectedText()
        else:
            word = self._text_under_cursor()
        self.emit(SIGNAL("findOcurrences(QString)"), word)

    def _unfold_blocks_for_jump(self, lineno):
        """Unfold the blocks previous to the lineno."""
        for line in self._sidebarWidget.foldedBlocks:
            if lineno >= line:
                self._sidebarWidget.code_folding_event(line + 1)
            else:
                break

    def go_to_line(self, lineno):
        """
        Go to an specific line
        """
        self._unfold_blocks_for_jump(lineno)
        if self.blockCount() >= lineno:
            cursor = self.textCursor()
            cursor.setPosition(self.document().findBlockByLineNumber(
                lineno).position())
            self.setTextCursor(cursor)

    def zoom_in(self):
        font = self.document().defaultFont()
        size = font.pointSize()
        if size < settings.FONT_MAX_SIZE:
            size += 2
            font.setPointSize(size)
        self.setFont(font)
        self._update_margin_line(font)

    def zoom_out(self):
        font = self.document().defaultFont()
        size = font.pointSize()
        if size > settings.FONT_MIN_SIZE:
            size -= 2
            font.setPointSize(size)
        self.setFont(font)
        self._update_margin_line(font)

    def _update_margin_line(self, font=None):
        if not font:
            font = self.document().defaultFont()
        # Fix for older version of Qt which doens't has ForceIntegerMetrics
        if "ForceIntegerMetrics" in dir(QFont):
            self.document().defaultFont().setStyleStrategy(
                QFont.ForceIntegerMetrics)
        font_metrics = QFontMetricsF(self.document().defaultFont())
        if (font_metrics.width("#") * settings.MARGIN_LINE) == \
           (font_metrics.width(" ") * settings.MARGIN_LINE):
            self.pos_margin = font_metrics.width('#') * settings.MARGIN_LINE
        else:
            char_width = font_metrics.averageCharWidth()
            self.pos_margin = char_width * settings.MARGIN_LINE

    def get_parent_project(self):
        return ''

    def get_cursor_position(self):
        return self.textCursor().position()

    def set_cursor_position(self, pos):
        if self.document().characterCount() >= pos:
            cursor = self.textCursor()
            cursor.setPosition(pos)
            self.setTextCursor(cursor)

    def indent_more(self):
        #cursor is a COPY all changes do not affect the QPlainTextEdit's cursor
        cursor = self.textCursor()
        #line where indent_more should start and end
        block = self.document().findBlock(
            cursor.selectionStart())
        end = self.document().findBlock(cursor.selectionEnd()).next()

        #Start a undo block
        cursor.beginEditBlock()

        #Move the COPY cursor
        cursor.setPosition(block.position())
        while block != end:
            cursor.setPosition(block.position())
            if self.useTabs:
                cursor.insertText('\t')
            else:
                cursor.insertText(' ' * self.indent)
            block = block.next()

        #End a undo block
        cursor.endEditBlock()

    def indent_less(self):
        #cursor is a COPY all changes do not affect the QPlainTextEdit's cursor
        cursor = self.textCursor()
        if not cursor.hasSelection():
            cursor.movePosition(QTextCursor.EndOfLine)
        #line where indent_less should start and end
        block = self.document().findBlock(
            cursor.selectionStart())
        end = self.document().findBlock(cursor.selectionEnd()).next()

        #Start a undo block
        cursor.beginEditBlock()

        cursor.setPosition(block.position())
        while block != end:
            cursor.setPosition(block.position())
            #Select Settings.indent chars from the current line
            if self.useTabs:
                cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor)
            else:
                cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor,
                    self.indent)
            text = cursor.selectedText()
            if not self.useTabs and text == ' ' * self.indent:
                cursor.removeSelectedText()
            elif self.useTabs and text == '\t':
                cursor.removeSelectedText()
            block = block.next()

        #End a undo block
        cursor.endEditBlock()

    def find_match(self, word, flags, findNext=False):
        flags = QTextDocument.FindFlags(flags)
        if findNext:
            self.moveCursor(QTextCursor.NoMove, QTextCursor.KeepAnchor)
        else:
            self.moveCursor(QTextCursor.StartOfWord, QTextCursor.KeepAnchor)
        found = self.find(word, flags)
        if not found:
            cursor = self.textCursor()
            self.moveCursor(QTextCursor.Start)
            found = self.find(word, flags)
            if not found:
                self.setTextCursor(cursor)
        if found:
            self.highlight_selected_word(word)

    def replace_match(self, wordOld, wordNew, flags, allwords=False,
                        selection=False):
        """Find if searched text exists and replace it with new one.
        If there is a selection just do it inside it and exit.
        """
        tc = self.textCursor()
        if selection and tc.hasSelection():
            tc.beginEditBlock()
            start, end = tc.selectionStart(), tc.selectionEnd()
            text = tc.selectedText()
            old_len = len(text)
            max_replace = -1  # all
            text = text.replace(wordOld, wordNew, max_replace)
            new_len = len(text)
            tc.insertText(text)
            offset = new_len - old_len
            self.__set_selection_from_pair(start, end + offset)
            tc.endEditBlock()
            return

        flags = QTextDocument.FindFlags(flags)

        cursor = self.textCursor()
        cursor.beginEditBlock()
        if allwords:
            current_pos = cursor.position()
            self.moveCursor(QTextCursor.Start)

        # replace is used for first replacement without selection.
        replace = True
        while (replace or allwords):
            result = self.find(wordOld, flags)

            if result:
                tc = self.textCursor()
                if tc.hasSelection():
                    tc.insertText(wordNew)
            else:
                break
            replace = False

        cursor.endEditBlock()
        if allwords:
            cursor.setPosition(current_pos)
            self.setTextCursor(cursor)

    def focusInEvent(self, event):
        super(Editor, self).focusInEvent(event)
        try:
            #use parent().parent() to Access QTabWidget
            #First parent() = QStackedWidget, Second parent() = TabWidget
            #Check for modifications
            self.parent().parent().focusInEvent(event)
        except RuntimeError:
            pass

    def focusOutEvent(self, event):
        """Hide Popup on focus lost."""
        self.completer.hide_completer()
        super(Editor, self).focusOutEvent(event)

    def resizeEvent(self, event):
        QPlainTextEdit.resizeEvent(self, event)
        self._sidebarWidget.setFixedHeight(self.height())
        if self._mini:
            self._mini.adjust_to_parent()

    def __insert_indentation(self, event):
        if self.textCursor().hasSelection():
            self.indent_more()
        elif self.useTabs:
            return False
        else:
            self.textCursor().insertText(' ' * self.indent)
        return True

    def __backspace(self, event):
        if self.textCursor().hasSelection() or self.useTabs:
            return False
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.StartOfLine, QTextCursor.KeepAnchor)
        text = cursor.selection().toPlainText()
        if (len(text) % self.indent == 0) and text.isspace():
            cursor.movePosition(QTextCursor.StartOfLine)
            cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor,
                self.indent)
            cursor.removeSelectedText()
            return True

    def __home_pressed(self, event):
        if event.modifiers() == Qt.ControlModifier:
            return False
        elif event.modifiers() == Qt.ShiftModifier:
            move = QTextCursor.KeepAnchor
        else:
            move = QTextCursor.MoveAnchor
        if self.textCursor().atBlockStart():
            self.moveCursor(QTextCursor.WordRight, move)
            return True

        cursor = self.textCursor()
        position = cursor.position()
        self.moveCursor(QTextCursor.StartOfLine, move)
        self.moveCursor(QTextCursor.WordRight, move)
        if position != self.textCursor().position() and \
           cursor.block().text().startswith((' ', '\t')):
            return True

    def __ignore_extended_line(self, event):
        if event.modifiers() == Qt.ShiftModifier:
            return True

    def __set_selection_from_pair(self, begin, end):
        """Set the current editor cursor with a selection from a given pair of
        positions"""
        cursor = self.textCursor()
        cursor.setPosition(begin)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

    def __reverse_select_text_portion_from_offset(self, begin, end):
        """Backwards select text, go from current+begin to current - end
        possition, returns text"""
        cursor = self.textCursor()
        cursor_position = cursor.position()
        cursor.setPosition(cursor_position + begin)
        #QT silently fails on invalid position, ergo breaks when EOF < begin
        while (cursor.position() == cursor_position) and begin > 0:
            begin -= 1
            cursor.setPosition(cursor_position + begin)
        cursor.setPosition(cursor_position - end, QTextCursor.KeepAnchor)
        selected_text = cursor.selectedText()
        return selected_text

    def __quot_completion(self, event):
        """Indicate if this is some sort of quote that needs to be completed
        This is a very simple boolean table, given that quotes are a
        simmetrical symbol, is a little more cumbersome guessing the completion
        table.
        """
        text = event.text()
        pos = self.textCursor().position()
        next_char = self.get_selection(pos, pos + 1).strip()
        if self.cursor_inside_string() and text == next_char:
            self.moveCursor(QTextCursor.Right)
            return True
        PENTA_Q = 5 * text
        TETRA_Q = 4 * text
        TRIPLE_Q = 3 * text
        DOUBLE_Q = 2 * text
        supress_echo = False
        pre_context = self.__reverse_select_text_portion_from_offset(0, 3)
        pos_context = self.__reverse_select_text_portion_from_offset(3, 0)
        if pre_context == pos_context == TRIPLE_Q:
            supress_echo = True
        elif pos_context[:2] == DOUBLE_Q:
            pre_context = self.__reverse_select_text_portion_from_offset(0, 4)
            if pre_context == TETRA_Q:
                supress_echo = True
        elif pos_context[:1] == text:
            pre_context = self.__reverse_select_text_portion_from_offset(0, 5)
            if pre_context == PENTA_Q:
                supress_echo = True
            elif pre_context[-1] == text:
                supress_echo = True
        if supress_echo:
            self.moveCursor(QTextCursor.Right)
        return supress_echo

    def __brace_completion(self, event):
        """Indicate if this symbol is part of a given pair and needs to be
        completed.
        """
        text = event.text()
        if text in list(settings.BRACES.values()):
            portion = self.__reverse_select_text_portion_from_offset(1, 1)
            brace_open = portion[0]
            brace_close = (len(portion) > 1) and portion[1] or None
            balance = BRACE_DICT.get(brace_open, None) == text == brace_close
            if balance:
                self.moveCursor(QTextCursor.Right)
                return True

    def __auto_indent(self, event):
        text = self.textCursor().block().previous().text()
        spaces = helpers.get_indentation(text, self.indent, self.useTabs)
        self.textCursor().insertText(spaces)
        if text != '' and text == ' ' * len(text):
            self.moveCursor(QTextCursor.Up)
            self.moveCursor(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)
            self.textCursor().removeSelectedText()
            self.moveCursor(QTextCursor.Down)
        elif settings.COMPLETE_DECLARATIONS:
            helpers.check_for_assistance_completion(self, text)
        cursor = self.textCursor()
        cursor.setPosition(cursor.position())
        self.setTextCursor(cursor)

    def complete_declaration(self):
        settings.COMPLETE_DECLARATIONS = not settings.COMPLETE_DECLARATIONS
        self.insert_new_line()
        settings.COMPLETE_DECLARATIONS = not settings.COMPLETE_DECLARATIONS

    def insert_new_line(self):
        cursor = self.textCursor()
        at_block_end = cursor.atBlockEnd()
        cursor.movePosition(QTextCursor.EndOfLine)
        cursor.insertBlock()
        if not at_block_end:
            self.moveCursor(QTextCursor.Down)
        self.__auto_indent(None)

    def __complete_braces(self, event):
        """Complete () [] and {} using a mild inteligence to see if corresponds
        and also do some more magic such as complete in classes and functions.
        """
        brace = event.text()
        if brace not in settings.BRACES:
            # Thou shalt not waste cpu cycles if this brace compleion dissabled
            return
        text = self.textCursor().block().text()
        complementary_brace = BRACE_DICT.get(brace)
        token_buffer = []
        _, tokens = self.__tokenize_text(text)
        is_unbalance = 0
        for tkn_type, tkn_rep, tkn_begin, tkn_end in tokens:
            if tkn_rep == brace:
                is_unbalance += 1
            elif tkn_rep == complementary_brace:
                is_unbalance -= 1
            if tkn_rep.strip() != "":
                token_buffer.append((tkn_rep, tkn_end[1]))
            is_unbalance = (is_unbalance >= 0) and is_unbalance or 0

        if (self.lang == "python") and (len(token_buffer) == 3) and \
                (token_buffer[2][0] == brace) and (token_buffer[0][0] in
                                                        ("def", "class")):
            #are we in presence of a function?
            self.textCursor().insertText("):")
            self.__fancyMoveCursor(QTextCursor.Left, 2)
            self.textCursor().insertText(self.selected_text)
        elif token_buffer and (not is_unbalance) and \
           self.selected_text:
            self.textCursor().insertText(self.selected_text)
        elif is_unbalance:
            pos = self.textCursor().position()
            next_char = self.get_selection(pos, pos + 1).strip()
            if self.selected_text or next_char == "":
                self.textCursor().insertText(complementary_brace)
                self.moveCursor(QTextCursor.Left)
                self.textCursor().insertText(self.selected_text)

    def __complete_quotes(self, event):
        """
        Completion for single and double quotes, which since are simmetrical
        symbols used for different things can not be balanced as easily as
        braces or equivalent.
        """
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.StartOfLine,
            QTextCursor.KeepAnchor)
        symbol = event.text()
        if symbol in settings.QUOTES:
            pre_context = self.__reverse_select_text_portion_from_offset(0, 3)
            if pre_context == 3 * symbol:
                self.textCursor().insertText(3 * symbol)
                self.__fancyMoveCursor(QTextCursor.Left, 3)
            else:
                self.textCursor().insertText(symbol)
                self.moveCursor(QTextCursor.Left)
            self.textCursor().insertText(self.selected_text)

    def keyPressEvent(self, event):
        #Completer pre key event
        if self.completer.process_pre_key_event(event):
            return
        #On Return == True stop the execution of this method
        if self.preKeyPress.get(event.key(), lambda x: False)(event):
            #emit a signal then plugings can do something
            self.emit(SIGNAL("keyPressEvent(QEvent)"), event)
            return
        self.selected_text = self.textCursor().selectedText()

        QPlainTextEdit.keyPressEvent(self, event)

        self.postKeyPress.get(event.key(), lambda x: False)(event)

        #Completer post key event
        self.completer.process_post_key_event(event)

        #emit a signal then plugings can do something
        self.emit(SIGNAL("keyPressEvent(QEvent)"), event)

    def keyReleaseEvent(self, event):
        QPlainTextEdit.keyReleaseEvent(self, event)
        block_number = self.textCursor().blockNumber()
        if block_number != self._last_block_position:
            self._last_block_position = block_number
            self.emit(SIGNAL("currentLineChanged(int)"), block_number)

    def _text_under_cursor(self):
        tc = self.textCursor()
        tc.select(QTextCursor.WordUnderCursor)
        word = tc.selectedText()
        result = self._patIsWord.findall(word)
        word = result[0] if result else ''
        return word

    def paintEvent(self, event):
        super(Editor, self).paintEvent(event)
        if settings.SHOW_MARGIN_LINE:
            painter = QPainter()
            painter.begin(self.viewport())
            opacity = resources.CUSTOM_SCHEME.get("margin-opacity",
                resources.COLOR_SCHEME["margin-opacity"])
            color_name = resources.CUSTOM_SCHEME.get("margin-line",
                resources.COLOR_SCHEME["margin-line"])
            painter.setPen(QColor(color_name))
            offset = self.contentOffset()
            width = self.viewport().width() - (self.pos_margin + offset.x())
            rect = QRect(self.pos_margin + offset.x(), 0,
                width, self.viewport().height())
            background = QColor(color_name)
            background.setAlpha(opacity)
            painter.fillRect(rect, background)
            painter.drawRect(rect)
            painter.end()

    def wheelEvent(self, event, forward=True):
        if event.modifiers() == Qt.ControlModifier:
            if event.delta() == 120:
                self.zoom_in()
            elif event.delta() == -120:
                self.zoom_out()
            event.ignore()
        QPlainTextEdit.wheelEvent(self, event)

    def contextMenuEvent(self, event):
        popup_menu = self.createStandardContextMenu()

        menu_lint = QMenu(self.tr("Ignore Lint"))
        ignoreLineAction = menu_lint.addAction(
            self.tr("Ignore This Line"))
        ignoreSelectedAction = menu_lint.addAction(
            self.tr("Ignore Selected Area"))
        self.connect(ignoreLineAction, SIGNAL("triggered()"),
            lambda: helpers.lint_ignore_line(self))
        self.connect(ignoreSelectedAction, SIGNAL("triggered()"),
            lambda: helpers.lint_ignore_selection(self))
        popup_menu.insertSeparator(popup_menu.actions()[0])
        popup_menu.insertMenu(popup_menu.actions()[0], menu_lint)
        popup_menu.insertAction(popup_menu.actions()[0],
            self.__actionFindOccurrences)
        #add extra menus (from Plugins)
        #lang = file_manager.get_file_extension(self.file_path)
        #extra_menus = self.EXTRA_MENU.get(lang, None)
        #if extra_menus:
            #popup_menu.addSeparator()
            #for menu in extra_menus:
                #popup_menu.addMenu(menu)
        #show menu
        popup_menu.exec_(event.globalPos())

    def mouseMoveEvent(self, event):
        position = event.pos()
        cursor = self.cursorForPosition(position)
        block = cursor.block()
        checkers = self._neditable.sorted_checkers
        for items in checkers:
            checker, color, _ = items
            if block.blockNumber() in checker.checks:
                message = checker.checks[block.blockNumber()][0]
                QToolTip.showText(self.mapToGlobal(position), message, self)
        if event.modifiers() == Qt.ControlModifier:
            cursor.select(QTextCursor.WordUnderCursor)
            selection_start = cursor.selectionStart()
            selection_end = cursor.selectionEnd()
            cursor.setPosition(selection_start - 1)
            cursor.setPosition(selection_end + 1, QTextCursor.KeepAnchor)
            if cursor.selectedText()[-1:] in ('(', '.') or \
            cursor.selectedText()[:1] in ('.', '@'):
                self.extraSelections = []
                selection = QTextEdit.ExtraSelection()
                lineColor = QColor(resources.CUSTOM_SCHEME.get('linkNavigate',
                            resources.COLOR_SCHEME['linkNavigate']))
                selection.format.setForeground(lineColor)
                selection.format.setFontUnderline(True)
                selection.cursor = cursor
                self.extraSelections.append(selection)
                self.setExtraSelections(self.extraSelections)
            else:
                self.extraSelections = []
                self.setExtraSelections(self.extraSelections)
        QPlainTextEdit.mouseMoveEvent(self, event)

    def mousePressEvent(self, event):
        if self.completer.isVisible():
            self.completer.hide_completer()
        if event.modifiers() == Qt.ControlModifier:
            cursor = self.cursorForPosition(event.pos())
            self.setTextCursor(cursor)
            self.go_to_definition(cursor)
        elif event.button() == Qt.RightButton and \
                not self.textCursor().hasSelection():
            cursor = self.cursorForPosition(event.pos())
            self.setTextCursor(cursor)
        QPlainTextEdit.mousePressEvent(self, event)

    def mouseReleaseEvent(self, event):
        QPlainTextEdit.mouseReleaseEvent(self, event)
        if event.button() == Qt.LeftButton:
            self.highlight_selected_word()
        block_number = self.textCursor().blockNumber()
        if block_number != self._last_block_position:
            self._last_block_position = block_number
            self.emit(SIGNAL("currentLineChanged(int)"), block_number)

    def dropEvent(self, event):
        if len(event.mimeData().urls()) > 0:
            path = event.mimeData().urls()[0].path()
            self.emit(SIGNAL("openDropFile(QString)"), path)
            event.ignore()
            event.mimeData = QMimeData()
        QPlainTextEdit.dropEvent(self, event)
        self.undo()

    def go_to_definition(self, cursor=None):
        if not cursor:
            cursor = self.textCursor()
        cursor.select(QTextCursor.WordUnderCursor)
        selection_start = cursor.selectionStart()
        selection_end = cursor.selectionEnd()
        cursor.setPosition(selection_start - 1)
        cursor.setPosition(selection_end + 1, QTextCursor.KeepAnchor)
        if cursor.selectedText().endswith('(') or \
           cursor.selectedText().startswith('@'):
            cursor.setPosition(selection_start)
            cursor.setPosition(selection_end, QTextCursor.KeepAnchor)
            self.emit(SIGNAL("locateFunction(QString, QString, bool)"),
                cursor.selectedText(), self.file_path, False)
        elif cursor.selectedText().endswith('.') or \
             cursor.selectedText().startswith('.'):
            cursor.setPosition(selection_start)
            cursor.setPosition(selection_end, QTextCursor.KeepAnchor)
            self.emit(SIGNAL("locateFunction(QString, QString, bool)"),
                cursor.selectedText(), self.file_path, True)

    def get_selection(self, posStart, posEnd):
        cursor = self.textCursor()
        cursor.setPosition(posStart)
        cursor2 = self.textCursor()
        if posEnd == QTextCursor.End:
            cursor2.movePosition(posEnd)
            cursor.setPosition(cursor2.position(), QTextCursor.KeepAnchor)
        else:
            cursor.setPosition(posEnd, QTextCursor.KeepAnchor)
        return cursor.selection().toPlainText()

    def __get_abs_position_on_text(self, text, position):
        """tokens give us position of char in a given line, we need
        such position relative to the beginning of the text, also we need
        to add the number of lines, since our split removes the newlines
        which are counted as a character in the editor"""
        line, relative_position = position
        insplit_line = line - 1
        full_lenght = 0
        for each_line in text.splitlines()[:insplit_line]:
            full_lenght += len(each_line)
        return full_lenght + insplit_line + relative_position

    def __fancyMoveCursor(self, operation, repeat=1,
                                            moveMode=QTextCursor.MoveAnchor):
        """Move the cursor a given number of times (with or without
        anchoring), just a helper given the less than practical way qt
        has for such a common operation"""
        cursor = self.textCursor()
        cursor.movePosition(operation, moveMode, repeat)
        self.setTextCursor(cursor)

    def __tokenize_text(self, text):
        invalid_syntax = False
        token_buffer = []
        try:
            for tkn_type, tkn_rep, tkn_begin, tkn_end, _ in \
                            generate_tokens(StringIO(text).readline):
                token_buffer.append((tkn_type, tkn_rep, tkn_begin, tkn_end))
        except (TokenError, IndentationError, SyntaxError):
            invalid_syntax = True
        return (invalid_syntax, token_buffer)

    def _match_braces(self, position, brace, forward):
        """Return the position to hilight of the matching brace"""
        braceMatch = BRACE_DICT[brace]
        if forward:
            text = self.get_selection(position, QTextCursor.End)
        else:
            text = self.get_selection(QTextCursor.Start, position)
        brace_stack = []
        brace_buffer = []
        invalid_syntax, tokens = self.__tokenize_text(text)
        for tkn_type, tkn_rep, tkn_begin, tkn_end in tokens:
            if (tkn_type == tkn.OP) and (tkn_rep in BRACE_DICT):
                tkn_pos = forward and tkn_begin or tkn_end
                brace_buffer.append((tkn_rep, tkn_pos))
        if not forward:
            brace_buffer.reverse()
        if forward and (not invalid_syntax):
            #Exclude the brace that triggered all this
            brace_buffer = brace_buffer[1:]

        for tkn_rep, tkn_position in brace_buffer:
            if (tkn_rep == braceMatch) and not brace_stack:
                hl_position = \
                self.__get_abs_position_on_text(text, tkn_position)
                return forward and hl_position + position or hl_position
            elif brace_stack and \
                (BRACE_DICT.get(tkn_rep, '') == brace_stack[-1]):
                brace_stack.pop(-1)
            else:
                brace_stack.append(tkn_rep)

    def highlight_current_line(self):
        self.emit(SIGNAL("cursorPositionChange(int, int)"),
            self.textCursor().blockNumber() + 1,
            self.textCursor().columnNumber())
        self.extraSelections = []

        if not self.isReadOnly():
            block = self.textCursor()
            selection = QTextEdit.ExtraSelection()
            lineColor = self._current_line_color
            lineColor.setAlpha(resources.CUSTOM_SCHEME.get(
                "current-line-opacity",
                resources.COLOR_SCHEME["current-line-opacity"]))

            checkers = self._neditable.sorted_checkers
            for items in checkers:
                checker, color, _ = items
                if block.blockNumber() in checker.checks:
                    lineColor = QColor(color)
                    lineColor.setAlpha(resources.CUSTOM_SCHEME.get(
                        "checker-background-opacity",
                        resources.COLOR_SCHEME["checker-background-opacity"]))
                    break

            selection.format.setBackground(lineColor)
            selection.format.setProperty(QTextFormat.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            self.extraSelections.append(selection)
        self.setExtraSelections(self.extraSelections)

        #Re-position tooltip to allow text editing in the line of the error
        if QToolTip.isVisible():
            QToolTip.hideText()

        if self._braces is not None:
            self._braces = None
        cursor = self.textCursor()
        if cursor.position() == 0:
            self.setExtraSelections(self.extraSelections)
            return
        cursor.movePosition(QTextCursor.PreviousCharacter,
                             QTextCursor.KeepAnchor)
        text = cursor.selectedText()
        pos1 = cursor.position()
        if text in (")", "]", "}"):
            pos2 = self._match_braces(pos1, text, forward=False)
        elif text in ("(", "[", "{"):
            pos2 = self._match_braces(pos1, text, forward=True)
        else:
            self.setExtraSelections(self.extraSelections)
            return
        if pos2 is not None:
            self._braces = (pos1, pos2)
            selection = QTextEdit.ExtraSelection()
            selection.format.setForeground(QColor(
                resources.CUSTOM_SCHEME.get('brace-foreground',
                resources.COLOR_SCHEME.get('brace-foreground'))))
            selection.cursor = cursor
            self.extraSelections.append(selection)
            selection = QTextEdit.ExtraSelection()
            selection.format.setForeground(QColor(
                resources.CUSTOM_SCHEME.get('brace-foreground',
                resources.COLOR_SCHEME.get('brace-foreground'))))
            selection.format.setBackground(QColor(
                resources.CUSTOM_SCHEME.get('brace-background',
                resources.COLOR_SCHEME.get('brace-background'))))
            selection.cursor = self.textCursor()
            selection.cursor.setPosition(pos2)
            selection.cursor.movePosition(QTextCursor.NextCharacter,
                             QTextCursor.KeepAnchor)
            self.extraSelections.append(selection)
        else:
            self._braces = (pos1,)
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(QColor(
                resources.CUSTOM_SCHEME.get('brace-background',
                resources.COLOR_SCHEME.get('brace-background'))))
            selection.format.setForeground(QColor(
                resources.CUSTOM_SCHEME.get('brace-foreground',
                resources.COLOR_SCHEME.get('brace-foreground'))))
            selection.cursor = cursor
            self.extraSelections.append(selection)
        self.setExtraSelections(self.extraSelections)

    def highlight_selected_word(self, word_find=None):
        #Highlight selected variable
        word = self._text_under_cursor()
        partial = False
        if word_find is not None:
            word = word_find
        if word != self._selected_word:
            self._selected_word = word
            if word_find:
                partial = True
            self.highlighter.set_selected_word(word, partial)
        elif (word == self._selected_word) and (word_find is None):
            self._selected_word = None
            self.highlighter.set_selected_word("", partial=True)
        elif (word == self._selected_word) and (word_find is not None):
            self.highlighter.set_selected_word(word_find, partial=True)

    def async_highlight(self):
        pass
        #self.highlighter.async_highlight()

    def to_upper(self):
        self.textCursor().beginEditBlock()
        if self.textCursor().hasSelection():
            text = self.textCursor().selectedText().upper()
        else:
            text = self._text_under_cursor().upper()
            self.moveCursor(QTextCursor.StartOfWord)
            self.moveCursor(QTextCursor.EndOfWord,
                QTextCursor.KeepAnchor)
        self.textCursor().insertText(text)
        self.textCursor().endEditBlock()

    def to_lower(self):
        self.textCursor().beginEditBlock()
        if self.textCursor().hasSelection():
            text = self.textCursor().selectedText().lower()
        else:
            text = self._text_under_cursor().lower()
            self.moveCursor(QTextCursor.StartOfWord)
            self.moveCursor(QTextCursor.EndOfWord,
                QTextCursor.KeepAnchor)
        self.textCursor().insertText(text)
        self.textCursor().endEditBlock()

    def to_title(self):
        self.textCursor().beginEditBlock()
        if self.textCursor().hasSelection():
            text = self.textCursor().selectedText().title()
        else:
            text = self._text_under_cursor().title()
            self.moveCursor(QTextCursor.StartOfWord)
            self.moveCursor(QTextCursor.EndOfWord, QTextCursor.KeepAnchor)
        self.textCursor().insertText(text)
        self.textCursor().endEditBlock()


def create_editor(neditable, syntax=None):
    editor = Editor(neditable)
    #if syntax is specified, use it
    if syntax:
        editor.register_syntax(syntax)
    else:
        #try to set a syntax based on the file extension
        #ext = file_manager.get_file_extension(neditable.ID)
        #if ext not in settings.EXTENSIONS and neditable.ID == '':
            #by default use python syntax
        editor.register_syntax('py')
        #else:
            #editor.register_syntax(ext)

    return editor
