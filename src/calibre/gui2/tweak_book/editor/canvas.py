#!/usr/bin/env python


__license__ = 'GPL v3'
__copyright__ = '2013, Kovid Goyal <kovid at kovidgoyal.net>'

import sys
import weakref
from functools import wraps
from io import BytesIO

from qt.core import (
    QApplication,
    QColor,
    QIcon,
    QImage,
    QImageWriter,
    QPainter,
    QPen,
    QPixmap,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTransform,
    QUndoCommand,
    QUndoStack,
    QWidget,
    pyqtSignal,
)

from calibre import fit_image
from calibre.gui2 import error_dialog, pixmap_to_data
from calibre.gui2.dnd import DownloadDialog, dnd_get_image, dnd_has_extension, dnd_has_image, image_extensions
from calibre.gui2.tweak_book import capitalize
from calibre.utils.img import (
    despeckle_image,
    gaussian_blur_image,
    gaussian_sharpen_image,
    image_to_data,
    normalize_image,
    oil_paint_image,
    remove_borders_from_image,
)
from calibre.utils.imghdr import identify


def painter(func):
    @wraps(func)
    def ans(self, painter):
        painter.save()
        try:
            return func(self, painter)
        finally:
            painter.restore()
    return ans


class SelectionState:

    __slots__ = ('current_mode', 'drag_corner', 'dragging', 'in_selection', 'last_drag_pos', 'last_press_point', 'rect')

    def __init__(self):
        self.reset()

    def reset(self, full=True):
        self.last_press_point = None
        if full:
            self.current_mode = None
            self.rect = None
        self.in_selection = False
        self.drag_corner = None
        self.dragging = None
        self.last_drag_pos = None


class Command(QUndoCommand):

    TEXT = ''

    def __init__(self, canvas):
        QUndoCommand.__init__(self, self.TEXT)
        self.canvas_ref = weakref.ref(canvas)
        self.before_image = i = canvas.current_image
        if i is None:
            raise ValueError('No image loaded')
        if i.isNull():
            raise ValueError('Cannot perform operations on invalid images')
        self.after_image = self(canvas)

    def undo(self):
        canvas = self.canvas_ref()
        canvas.set_image(self.before_image)

    def redo(self):
        canvas = self.canvas_ref()
        canvas.set_image(self.after_image)


def get_selection_rect(img, sr, target):
    ' Given selection rect return the corresponding rectangle in the underlying image as left, top, width, height '
    left_border = (abs(sr.left() - target.left())/target.width()) * img.width()
    top_border = (abs(sr.top() - target.top())/target.height()) * img.height()
    right_border = (abs(target.right() - sr.right())/target.width()) * img.width()
    bottom_border = (abs(target.bottom() - sr.bottom())/target.height()) * img.height()
    return left_border, top_border, img.width() - left_border - right_border, img.height() - top_border - bottom_border


class Trim(Command):
    ''' Remove the areas of the image outside the current selection. '''

    TEXT = _('Trim image')

    def __call__(self, canvas):
        return canvas.current_image.copy(*map(int, canvas.rect_for_trim()))


class AutoTrim(Trim):
    ''' Auto trim borders from the image '''
    TEXT = _('Auto-trim image')

    def __call__(self, canvas):
        return remove_borders_from_image(canvas.current_image)


class Rotate(Command):

    TEXT = _('Rotate image')

    def __call__(self, canvas):
        img = canvas.current_image
        m = QTransform()
        m.rotate(90)
        return img.transformed(m, Qt.TransformationMode.SmoothTransformation)


class Scale(Command):

    TEXT = _('Resize image')

    def __init__(self, width, height, canvas):
        self.width, self.height = width, height
        Command.__init__(self, canvas)

    def __call__(self, canvas):
        img = canvas.current_image
        return img.scaled(self.width, self.height, transformMode=Qt.TransformationMode.SmoothTransformation)


class Sharpen(Command):

    TEXT = _('Sharpen image')
    FUNC = 'sharpen'

    def __init__(self, sigma, canvas):
        self.sigma = sigma
        Command.__init__(self, canvas)

    def __call__(self, canvas):
        return gaussian_sharpen_image(canvas.current_image, sigma=self.sigma)


class Blur(Sharpen):

    TEXT = _('Blur image')
    FUNC = 'blur'

    def __call__(self, canvas):
        return gaussian_blur_image(canvas.current_image, sigma=self.sigma)


class Oilify(Command):

    TEXT = _('Make image look like an oil painting')

    def __init__(self, radius, canvas):
        self.radius = radius
        Command.__init__(self, canvas)

    def __call__(self, canvas):
        return oil_paint_image(canvas.current_image, radius=self.radius)


class Despeckle(Command):

    TEXT = _('De-speckle image')

    def __call__(self, canvas):
        return despeckle_image(canvas.current_image)


class Normalize(Command):

    TEXT = _('Normalize image')

    def __call__(self, canvas):
        return normalize_image(canvas.current_image)


class Replace(Command):
    ''' Replace the current image with another image. If there is a selection,
    only the region of the selection is replaced. '''

    def __init__(self, img, text, canvas):
        self.after_image = img
        self.TEXT = text
        Command.__init__(self, canvas)

    def __call__(self, canvas):
        if canvas.has_selection and canvas.selection_state.rect is not None:
            pimg = self.after_image
            img = self.after_image = QImage(canvas.current_image)
            rect = QRectF(*get_selection_rect(img, canvas.selection_state.rect, canvas.target))
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawImage(rect, pimg, QRectF(pimg.rect()))
            p.end()
        return self.after_image


def imageop(func):
    @wraps(func)
    def ans(self, *args, **kwargs):
        if self.original_image_data is None:
            return error_dialog(self, _('No image'), _('No image loaded'), show=True)
        if not self.is_valid:
            return error_dialog(self, _('Invalid image'), _('The current image is not valid'), show=True)
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            return func(self, *args, **kwargs)
        finally:
            QApplication.restoreOverrideCursor()
    return ans


class Canvas(QWidget):

    BACKGROUND = QColor(60, 60, 60)
    SHADE_COLOR = QColor(0, 0, 0, 180)
    SELECT_PEN = QPen(QColor(Qt.GlobalColor.white))

    selection_state_changed = pyqtSignal(object)
    selection_area_changed = pyqtSignal(object)
    undo_redo_state_changed = pyqtSignal(object, object)
    image_changed = pyqtSignal(object)

    @property
    def has_selection(self):
        return self.selection_state.current_mode == 'selected'

    @property
    def is_modified(self):
        return self.current_image is not self.original_image

    # Drag 'n drop {{{

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if dnd_has_extension(md, image_extensions()) or dnd_has_image(md):
            event.acceptProposedAction()

    def dropEvent(self, event):
        event.setDropAction(Qt.DropAction.CopyAction)
        md = event.mimeData()

        x, y = dnd_get_image(md)
        if x is not None:
            # We have an image, set cover
            event.accept()
            if y is None:
                # Local image
                self.undo_stack.push(Replace(x.toImage(), _('Drop image'), self))
            else:
                d = DownloadDialog(x, y, self.gui)
                d.start_download()
                if d.err is None:
                    with open(d.fpath, 'rb') as f:
                        img = QImage()
                        img.loadFromData(f.read())
                    if not img.isNull():
                        self.undo_stack.push(Replace(img, _('Drop image'), self))

        event.accept()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()
    # }}}

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.selection_state = SelectionState()
        self.undo_stack = u = QUndoStack()
        u.setUndoLimit(10)
        u.canUndoChanged.connect(self.emit_undo_redo_state)
        u.canRedoChanged.connect(self.emit_undo_redo_state)

        self.original_image_data = None
        self.is_valid = False
        self.original_image_format = None
        self.current_image = None
        self.current_scaled_pixmap = None
        self.last_canvas_size = None
        self.target = QRectF(0, 0, 0, 0)

        self.undo_action = a = self.undo_stack.createUndoAction(self, _('Undo') + ' ')
        a.setIcon(QIcon.ic('edit-undo.png'))
        self.redo_action = a = self.undo_stack.createRedoAction(self, _('Redo') + ' ')
        a.setIcon(QIcon.ic('edit-redo.png'))

    def load_image(self, data, only_if_different=False):
        if only_if_different and self.original_image_data and not self.is_modified and self.original_image_data == data:
            return
        self.is_valid = False
        try:
            fmt = identify(data)[0].encode('ascii')
        except Exception:
            fmt = b''
        self.original_image_format = fmt.decode('ascii').lower()
        self.selection_state.reset()
        self.original_image_data = data
        self.current_image = i = self.original_image = (
            QImage.fromData(data, format=fmt) if fmt else QImage.fromData(data))
        self.is_valid = not i.isNull()
        self.current_scaled_pixmap = None
        self.update()
        self.image_changed.emit(self.current_image)

    def set_image(self, qimage):
        self.selection_state.reset()
        self.current_scaled_pixmap = None
        self.current_image = qimage
        self.is_valid = not qimage.isNull()
        self.update()
        self.image_changed.emit(self.current_image)

    def get_image_data(self, quality=90):
        if not self.is_modified:
            return self.original_image_data
        fmt = self.original_image_format or 'JPEG'
        if fmt.lower() not in {x.data().decode('utf-8') for x in QImageWriter.supportedImageFormats()}:
            if fmt.lower() == 'gif':
                data = image_to_data(self.current_image, fmt='PNG', png_compression_level=0)
                from PIL import Image
                i = Image.open(BytesIO(data))
                buf = BytesIO()
                i.save(buf, 'gif')
                return buf.getvalue()
            else:
                raise ValueError(f'Cannot save {fmt} format images')
        return pixmap_to_data(self.current_image, format=fmt, quality=90)

    def copy(self):
        if not self.is_valid:
            return
        clipboard = QApplication.clipboard()
        if not self.has_selection or self.selection_state.rect is None:
            clipboard.setImage(self.current_image)
        else:
            trim = Trim(self)
            clipboard.setImage(trim.after_image)
            trim.before_image = trim.after_image = None

    def paste(self):
        clipboard = QApplication.clipboard()
        md = clipboard.mimeData()
        if md.hasImage():
            img = QImage(md.imageData())
            if not img.isNull():
                self.undo_stack.push(Replace(img, _('Paste image'), self))
        else:
            error_dialog(self, _('No image'), _(
                'No image available in the clipboard'), show=True)

    def break_cycles(self):
        self.undo_stack.clear()
        self.original_image_data = self.current_image = self.current_scaled_pixmap = None

    def emit_undo_redo_state(self):
        self.undo_redo_state_changed.emit(self.undo_action.isEnabled(), self.redo_action.isEnabled())

    @imageop
    def trim_image(self):
        if self.selection_state.rect is None:
            error_dialog(self, _('No selection'), _(
                'No active selection, first select a region in the image, by dragging with your mouse'), show=True)
            return False
        self.undo_stack.push(Trim(self))
        return True

    @imageop
    def autotrim_image(self):
        self.undo_stack.push(AutoTrim(self))
        return True

    @imageop
    def rotate_image(self):
        self.undo_stack.push(Rotate(self))
        return True

    @imageop
    def resize_image(self, width, height):
        self.undo_stack.push(Scale(width, height, self))
        return True

    @imageop
    def sharpen_image(self, sigma=3.0):
        self.undo_stack.push(Sharpen(sigma, self))
        return True

    @imageop
    def blur_image(self, sigma=3.0):
        self.undo_stack.push(Blur(sigma, self))
        return True

    @imageop
    def despeckle_image(self):
        self.undo_stack.push(Despeckle(self))
        return True

    @imageop
    def normalize_image(self):
        self.undo_stack.push(Normalize(self))
        return True

    @imageop
    def oilify_image(self, radius=4.0):
        self.undo_stack.push(Oilify(radius, self))
        return True

    # The selection rectangle {{{
    @property
    def dc_size(self):
        sr = self.selection_state.rect
        dx = min(75, sr.width() / 4)
        dy = min(75, sr.height() / 4)
        return dx, dy

    def get_drag_corner(self, pos):
        dx, dy = self.dc_size
        sr = self.selection_state.rect
        x, y = pos.x(), pos.y()
        hedge = 'left' if x < sr.x() + dx else 'right' if x > sr.right() - dx else None
        vedge = 'top' if y < sr.y() + dy else 'bottom' if y > sr.bottom() - dy else None
        return (hedge, vedge) if hedge or vedge else None

    def get_drag_rect(self):
        sr = self.selection_state.rect
        dc = self.selection_state.drag_corner
        if None in (sr, dc):
            return
        dx, dy = self.dc_size
        if None in dc:
            # An edge
            if dc[0] is None:
                top = sr.top() if dc[1] == 'top' else sr.bottom() - dy
                ans = QRectF(sr.left() + dx, top, sr.width() - 2 * dx, dy)
            else:
                left = sr.left() if dc[0] == 'left' else sr.right() - dx
                ans = QRectF(left, sr.top() + dy, dx, sr.height() - 2 * dy)
        else:
            # A corner
            left = sr.left() if dc[0] == 'left' else sr.right() - dx
            top = sr.top() if dc[1] == 'top' else sr.bottom() - dy
            ans = QRectF(left, top, dx, dy)
        return ans

    def get_cursor(self):
        dc = self.selection_state.drag_corner
        if dc is None:
            ans = Qt.CursorShape.OpenHandCursor if self.selection_state.last_drag_pos is None else Qt.CursorShape.ClosedHandCursor
        elif None in dc:
            ans = Qt.CursorShape.SizeVerCursor if dc[0] is None else Qt.CursorShape.SizeHorCursor
        else:
            ans = Qt.CursorShape.SizeBDiagCursor if dc in {('left', 'bottom'), ('right', 'top')} else Qt.CursorShape.SizeFDiagCursor
        return ans

    def update(self):
        super().update()
        self.selection_area_changed.emit(self.selection_state.rect)

    def move_edge(self, edge, dp):
        sr = self.selection_state.rect
        horiz = edge in {'left', 'right'}
        func = getattr(sr, 'set' + capitalize(edge))
        delta = getattr(dp, 'x' if horiz else 'y')()
        buf = 50
        if horiz:
            minv = self.target.left() if edge == 'left' else sr.left() + buf
            maxv = sr.right() - buf if edge == 'left' else self.target.right()
        else:
            minv = self.target.top() if edge == 'top' else sr.top() + buf
            maxv = sr.bottom() - buf if edge == 'top' else self.target.bottom()
        func(max(minv, min(maxv, delta + getattr(sr, edge)())))

    def move_selection_rect(self, x, y):
        sr = self.selection_state.rect
        half_width = sr.width() / 2.0
        half_height = sr.height() / 2.0
        c = sr.center()
        nx = c.x() + x
        ny = c.y() + y
        minx = self.target.left() + half_width
        maxx = self.target.right() - half_width
        miny, maxy = self.target.top() + half_height, self.target.bottom() - half_height
        nx = max(minx, min(maxx, nx))
        ny = max(miny, min(maxy, ny))
        sr.moveCenter(QPointF(nx, ny))

    def preserve_aspect_ratio_after_move(self, orig_rect, hedge, vedge):
        r = self.selection_state.rect

        def is_equal(a, b):
            return abs(a - b) < 0.001

        if vedge is None:
            new_height = r.width() * (orig_rect.height() / orig_rect.width())
            if not is_equal(new_height, r.height()):
                delta = (r.height() - new_height) / 2.0
                r.setTop(max(self.target.top(), r.top() + delta))
                r.setBottom(min(r.bottom() - delta, self.target.bottom()))
                if not is_equal(new_height, r.height()):
                    new_width = r.height() * (orig_rect.width() / orig_rect.height())
                    delta = r.width() - new_width
                    r.setLeft(r.left() + delta) if hedge == 'left' else r.setRight(r.right() - delta)
        elif hedge is None:
            new_width = r.height() * (orig_rect.width() / orig_rect.height())
            if not is_equal(new_width, r.width()):
                delta = (r.width() - new_width) / 2.0
                r.setLeft(max(self.target.left(), r.left() + delta))
                r.setRight(min(r.right() - delta, self.target.right()))
                if not is_equal(new_width, r.width()):
                    new_height = r.width() * (orig_rect.height() / orig_rect.width())
                    delta = r.height() - new_height
                    r.setTop(r.top() + delta) if hedge == 'top' else r.setBottom(r.bottom() - delta)
        else:
            buf = 50

            def set_width(new_width):
                if hedge == 'left':
                    r.setLeft(max(self.target.left(), min(r.right() - new_width, r.right() - buf)))
                else:
                    r.setRight(max(r.left() + buf, min(r.left() + new_width, self.target.right())))

            def set_height(new_height):
                if vedge == 'top':
                    r.setTop(max(self.target.top(), min(r.bottom() - new_height, r.bottom() - buf)))
                else:
                    r.setBottom(max(r.top() + buf, min(r.top() + new_height, self.target.bottom())))

            fractional_h, fractional_v = r.width() / orig_rect.width(), r.height() / orig_rect.height()
            smaller_frac = fractional_h if abs(fractional_h - 1) < abs(fractional_v - 1) else fractional_v
            set_width(orig_rect.width() * smaller_frac)
            frac = r.width() / orig_rect.width()
            set_height(orig_rect.height() * frac)
            if r.height() / orig_rect.height() != frac:
                set_width(orig_rect.width() * frac)

    def move_selection(self, dp, preserve_aspect_ratio=False):
        dm = self.selection_state.dragging
        if dm is None:
            self.move_selection_rect(dp.x(), dp.y())
        else:
            orig = QRectF(self.selection_state.rect)
            for edge in dm:
                if edge is not None:
                    self.move_edge(edge, dp)
            if preserve_aspect_ratio and dm:
                self.preserve_aspect_ratio_after_move(orig, dm[0], dm[1])

    def rect_for_trim(self):
        img = self.current_image
        target = self.target
        sr = self.selection_state.rect
        return get_selection_rect(img, sr, target)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.target.contains(ev.position()):
            pos = ev.position()
            self.selection_state.last_press_point = pos
            if self.selection_state.current_mode is None:
                self.selection_state.current_mode = 'select'

            elif self.selection_state.current_mode == 'selected':
                if self.selection_state.rect is not None and self.selection_state.rect.contains(pos):
                    self.selection_state.drag_corner = self.selection_state.dragging = self.get_drag_corner(pos)
                    self.selection_state.last_drag_pos = pos
                    self.setCursor(self.get_cursor())
                else:
                    self.selection_state.current_mode = 'select'
                    self.selection_state.rect = None
                    self.selection_state_changed.emit(False)
    @property
    def selection_rect_in_image_coords(self):
        if self.selection_state.current_mode == 'selected':
            left, top, width, height = self.rect_for_trim()
            return QRect(0, 0, int(width), int(height))
        return self.current_image.rect()

    def set_selection_size_in_image_coords(self, width, height):
        self.selection_state.reset()
        i = self.current_image
        self.selection_state.rect = QRectF(self.target.left(), self.target.top(),
                                           width * self.target.width() / i.width(), height * self.target.height() / i.height())
        self.selection_state.current_mode = 'selected'
        self.update()
        self.selection_state_changed.emit(self.has_selection)

    def mouseMoveEvent(self, ev):
        changed = False
        if self.selection_state.in_selection:
            changed = True
        self.selection_state.in_selection = False
        self.selection_state.drag_corner = None
        pos = ev.position()
        cursor = Qt.CursorShape.ArrowCursor
        try:
            if ev.buttons() & Qt.MouseButton.LeftButton:
                if self.selection_state.last_press_point is not None and self.selection_state.current_mode is not None:
                    if self.selection_state.current_mode == 'select':
                        r = QRectF(self.selection_state.last_press_point, pos).normalized()
                        r = r.intersected(self.target)
                        self.selection_state.rect = r
                        changed = True
                    elif self.selection_state.last_drag_pos is not None:
                        self.selection_state.in_selection = True
                        self.selection_state.drag_corner = self.selection_state.dragging
                        dp = pos - self.selection_state.last_drag_pos
                        self.selection_state.last_drag_pos = pos
                        self.move_selection(dp, preserve_aspect_ratio=ev.modifiers() & Qt.KeyboardModifier.AltModifier == Qt.KeyboardModifier.AltModifier)
                        cursor = self.get_cursor()
                        changed = True
            else:
                if not self.target.contains(QPointF(pos)) or self.selection_state.rect is None or not self.selection_state.rect.contains(QPointF(pos)):
                    return
                if self.selection_state.current_mode == 'selected':
                    if self.selection_state.rect is not None and self.selection_state.rect.contains(QPointF(pos)):
                        self.selection_state.drag_corner = self.get_drag_corner(pos)
                        self.selection_state.in_selection = True
                        cursor = self.get_cursor()
                        changed = True
        finally:
            if changed:
                self.update()
            self.setCursor(cursor)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.selection_state.dragging = self.selection_state.last_drag_pos = None
            if self.selection_state.current_mode == 'select':
                r = self.selection_state.rect
                if r is None or max(r.width(), r.height()) < 3:
                    self.selection_state.reset()
                else:
                    self.selection_state.current_mode = 'selected'
                self.selection_state_changed.emit(self.has_selection)
            elif self.selection_state.current_mode == 'selected' and self.selection_state.rect is not None and self.selection_state.rect.contains(
                    ev.position()):
                self.setCursor(self.get_cursor())
            self.update()

    def keyPressEvent(self, ev):
        k = ev.key()
        if k in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down) and self.selection_state.rect is not None and self.has_selection:
            ev.accept()
            delta = 10 if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            x = y = 0
            if k in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                x = delta * (-1 if k == Qt.Key.Key_Left else 1)
            else:
                y = delta * (-1 if k == Qt.Key.Key_Up else 1)
            self.move_selection_rect(x, y)
            self.update()
        else:
            return QWidget.keyPressEvent(self, ev)
    # }}}

    # Painting {{{
    @painter
    def draw_background(self, painter):
        painter.fillRect(self.rect(), self.BACKGROUND)

    @painter
    def draw_image_error(self, painter):
        font = painter.font()
        font.setPointSize(3 * font.pointSize())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(Qt.GlobalColor.black))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, _('Not a valid image'))

    def load_pixmap(self):
        canvas_size = self.rect().width(), self.rect().height()
        if self.last_canvas_size != canvas_size:
            if self.last_canvas_size is not None and self.selection_state.rect is not None:
                self.selection_state.reset()
                # TODO: Migrate the selection rect
            self.last_canvas_size = canvas_size
            self.current_scaled_pixmap = None
        if self.current_scaled_pixmap is None:
            pwidth, pheight = self.last_canvas_size
            i = self.current_image
            width, height = i.width(), i.height()
            scaled, width, height = fit_image(width, height, pwidth, pheight)
            try:
                dpr = self.devicePixelRatioF()
            except AttributeError:
                dpr = self.devicePixelRatio()
            if scaled:
                i = self.current_image.scaled(int(dpr * width), int(dpr * height), transformMode=Qt.TransformationMode.SmoothTransformation)
            self.current_scaled_pixmap = QPixmap.fromImage(i)
            self.current_scaled_pixmap.setDevicePixelRatio(dpr)

    @painter
    def draw_pixmap(self, painter):
        p = self.current_scaled_pixmap
        try:
            dpr = self.devicePixelRatioF()
        except AttributeError:
            dpr = self.devicePixelRatio()
        width, height = int(p.width()/dpr), int(p.height()/dpr)
        pwidth, pheight = self.last_canvas_size
        x = int(abs(pwidth - width)/2.)
        y = int(abs(pheight - height)/2.)
        self.target = QRectF(x, y, width, height)
        painter.drawPixmap(self.target, p, QRectF(p.rect()))

    @painter
    def draw_selection_rect(self, painter):
        cr, sr = self.target, self.selection_state.rect
        painter.setPen(self.SELECT_PEN)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if self.selection_state.current_mode == 'selected':
            # Shade out areas outside the selection rect
            for r in (
                QRectF(cr.topLeft(), QPointF(sr.left(), cr.bottom())),  # left
                QRectF(QPointF(sr.left(), cr.top()), sr.topRight()),  # top
                QRectF(QPointF(sr.right(), cr.top()), cr.bottomRight()),  # right
                QRectF(sr.bottomLeft(), QPointF(sr.right(), cr.bottom())),  # bottom
            ):
                painter.fillRect(r, self.SHADE_COLOR)

            dr = self.get_drag_rect()
            if self.selection_state.in_selection and dr is not None:
                # Draw the resize rectangle
                painter.save()
                painter.setCompositionMode(QPainter.CompositionMode.RasterOp_SourceAndNotDestination)
                painter.setClipRect(sr.adjusted(1, 1, -1, -1))
                painter.drawRect(dr)
                painter.restore()

        # Draw the selection rectangle
        painter.setCompositionMode(QPainter.CompositionMode.RasterOp_SourceAndNotDestination)
        painter.drawRect(sr)

    def paintEvent(self, event):
        QWidget.paintEvent(self, event)
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        try:
            self.draw_background(p)
            if self.original_image_data is None:
                return
            if not self.is_valid:
                return self.draw_image_error(p)
            self.load_pixmap()
            self.draw_pixmap(p)
            if self.selection_state.rect is not None:
                self.draw_selection_rect(p)
        finally:
            p.end()
    # }}}


if __name__ == '__main__':
    app = QApplication([])
    with open(sys.argv[-1], 'rb') as f:
        data = f.read()
    c = Canvas()
    c.load_image(data)
    c.show()
    app.exec()
