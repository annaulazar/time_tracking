import sqlite3
from datetime import date, timedelta
from pathlib import Path

import AppKit as kit
import objc
from Foundation import NSAttributedString, NSDate, NSLocale, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

from time_tracking.tracker import (
    MICROSECONDS_PER_SECOND,
    TimeTracker,
    TrackerAlreadyOpen,
    format_duration,
    start_of_day,
)


APP_NAME = "Учёт времени"
DB_PATH = Path.home() / "Library" / "Application Support" / "Time Tracking" / "time_tracking.sqlite3"
COMPACT_HINT = "Перетащите кнопку мышью. Правый клик или ⌘⇧M — развернуть окно."
CAPTION_SIZE = 20
TOTAL_SIZE = 25


def rgb(red, green, blue, alpha=1):
    return kit.NSColor.colorWithSRGBRed_green_blue_alpha_(red / 255, green / 255, blue / 255, alpha)


def compact_toggle_gray(appearance):
    dark = kit.NSAppearanceNameDarkAqua
    if appearance.bestMatchFromAppearancesWithNames_((kit.NSAppearanceNameAqua, dark)) == dark:
        return rgb(66, 66, 70)
    return rgb(226, 226, 230)


def button_shadow(color, offset, blur):
    shadow = kit.NSShadow.alloc().init()
    shadow.setShadowColor_(color)
    shadow.setShadowOffset_(offset)
    shadow.setShadowBlurRadius_(blur)
    return shadow


COMPACT_TOGGLE_GRAY = kit.NSColor.colorWithName_dynamicProvider_(None, compact_toggle_gray)
RUNNING_GRADIENT = kit.NSGradient.alloc().initWithStartingColor_endingColor_(rgb(250, 75, 110), rgb(208, 85, 178))
STOPPED_GRADIENT = kit.NSGradient.alloc().initWithStartingColor_endingColor_(rgb(93, 157, 232), rgb(80, 185, 174))


def label(view, title, frame, size=13, weight=kit.NSFontWeightRegular, *, secondary=False, digits=False):
    field = kit.NSTextField.labelWithString_(title)
    font = (
        kit.NSFont.monospacedDigitSystemFontOfSize_weight_
        if digits
        else kit.NSFont.systemFontOfSize_weight_
    )
    field.setFont_(font(size, weight))
    if secondary:
        field.setTextColor_(kit.NSColor.secondaryLabelColor())
    field.setFrame_(frame)
    view.addSubview_(field)
    return field


class RoundedButton(kit.NSButton):
    shadow_padding = 4
    gradient_shadow = button_shadow(rgb(43, 64, 108, 0.32), (0, -2), 3)

    def drawRect_(self, rect):
        gradient = isinstance(self.fill, kit.NSGradient)
        bounds = self.bounds()
        if gradient:
            bounds = kit.NSInsetRect(bounds, self.shadow_padding, self.shadow_padding)
        path = kit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, self.corner_radius, self.corner_radius)
        if gradient:
            kit.NSGraphicsContext.saveGraphicsState()
            self.gradient_shadow.set()
            kit.NSColor.whiteColor().setFill()
            path.fill()
            kit.NSGraphicsContext.restoreGraphicsState()
            self.fill.drawInBezierPath_angle_(path, -14 if self.isFlipped() else 14)
        else:
            self.fill.setFill()
            path.fill()
        if self.isHighlighted():
            kit.NSColor.blackColor().colorWithAlphaComponent_(0.12).setFill()
            path.fill()
        objc.super(RoundedButton, self).drawRect_(rect)

    @objc.python_method
    def set_style(self, title, fill):
        if self.title() == title and self.fill == fill:
            return
        self.fill = fill
        attributes = {
            kit.NSFontAttributeName: self.font(),
            kit.NSForegroundColorAttributeName: self.text_color,
        }
        self.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attributes))
        self.setNeedsDisplay_(True)


class CompactButton(RoundedButton):
    shadow_padding = 12
    gradient_shadow = button_shadow(rgb(57, 84, 137, 0.36), (0, -4), 5)
    press = None

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        if self.isEnabled():
            window = self.window()
            self.press = (window.convertPointToScreen_(event.locationInWindow()), window.frame().origin)
            self.dragging = False
            self.highlight_(True)

    def mouseDragged_(self, event):
        if self.press is None:
            return
        start, origin = self.press
        window = self.window()
        point = window.convertPointToScreen_(event.locationInWindow())
        dx, dy = point.x - start.x, point.y - start.y
        if not self.dragging and max(abs(dx), abs(dy)) < 4:
            return
        self.dragging = True
        self.highlight_(False)
        window.setFrameOrigin_((origin.x + dx, origin.y + dy))

    def mouseUp_(self, event):
        if self.press is None:
            return
        self.press = None
        self.highlight_(False)
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if not self.dragging and kit.NSPointInRect(point, self.bounds()):
            self.sendAction_to_(self.action(), self.target())


def button(view, title, frame, target, action, *, font_size, fill, text_color, corner_radius=8, button_class=RoundedButton):
    control = button_class.buttonWithTitle_target_action_("", target, action)
    control.fill = None
    control.text_color = text_color
    control.corner_radius = corner_radius
    control.setBordered_(False)
    control.setFont_(kit.NSFont.systemFontOfSize_weight_(font_size, kit.NSFontWeightSemibold))
    control.set_style(title, fill)
    control.setFrame_(frame)
    view.addSubview_(control)
    return control


def card(view, frame):
    box = kit.NSBox.alloc().initWithFrame_(frame)
    box.setBoxType_(kit.NSBoxCustom)
    box.setContentViewMargins_((0, 0))
    box.setCornerRadius_(12)
    box.setBorderWidth_(0)
    box.setFillColor_(kit.NSColor.controlBackgroundColor())
    view.addSubview_(box)
    return box.contentView()


def date_picker(view, initial_date, name, frame, target):
    picker = kit.NSDatePicker.alloc().initWithFrame_(frame)
    picker.setDatePickerStyle_(kit.NSDatePickerStyleTextFieldAndStepper)
    picker.setDatePickerElements_(kit.NSDatePickerElementFlagYearMonthDay)
    picker.setLocale_(NSLocale.localeWithLocaleIdentifier_("ru_RU"))
    initial_timestamp = start_of_day(initial_date) / MICROSECONDS_PER_SECOND
    picker.setDateValue_(NSDate.dateWithTimeIntervalSince1970_(initial_timestamp))
    picker.setAccessibilityLabel_(name)
    picker.setTarget_(target)
    picker.setAction_("periodChanged:")
    view.addSubview_(picker)
    return picker


def show_alert(message, detail):
    alert = kit.NSAlert.alloc().init()
    alert.setAlertStyle_(kit.NSAlertStyleWarning)
    alert.setMessageText_(message)
    alert.setInformativeText_(detail)
    alert.addButtonWithTitle_("ОК")
    alert.runModal()


class CompactPanel(kit.NSPanel):
    def canBecomeKeyWindow(self):
        return True


class AppController(NSObject):
    @objc.python_method
    def configure(self, app, tracker):
        self.app = app
        self.tracker = tracker
        self.timer = None
        self.compact = False
        self.panel_positioned = False
        self._build_main_window()
        self._build_compact_panel()
        self._build_menus()

    @objc.python_method
    def _build_main_window(self):
        style = kit.NSWindowStyleMaskTitled | kit.NSWindowStyleMaskClosable | kit.NSWindowStyleMaskMiniaturizable
        content_width = 648
        self.window = kit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (content_width + 48, 444)), style, kit.NSBackingStoreBuffered, False
        )
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitle_(APP_NAME)
        self.window.setDelegate_(self)
        self.window.center()
        view = self.window.contentView()

        label(view, "Рабочее время", ((24, 392), (275, 33)), 27, kit.NSFontWeightSemibold)
        self.status = label(view, "", ((25, 355), (330, 28)), CAPTION_SIZE, secondary=True)
        compact = button(
            view, "Компактно", ((504, 386), (168, 45)), self, "switchView:",
            font_size=19.5, fill=COMPACT_TOGGLE_GRAY, text_color=kit.NSColor.labelColor(),
        )
        compact.setToolTip_("Свернуть окно до кнопки таймера")

        for index, title in enumerate(("Сегодня", "На этой неделе")):
            summary = card(view, ((24 + index * 330, 266), (318, 79)))
            label(summary, title, ((16, 45), (286, 27)), CAPTION_SIZE, secondary=True)
            value = label(summary, "", ((16, 10), (286, 33)), TOTAL_SIZE, kit.NSFontWeightSemibold, digits=True)
            if index == 0:
                self.today_total = value
            else:
                self.week_total = value

        report = card(view, ((24, 110), (content_width, 144)))
        report_scale = 1.5
        report.scaleUnitSquareToSize_((report_scale, report_scale))
        label(report, "За выбранный период", ((16, 56), (239, 22)), CAPTION_SIZE / report_scale, kit.NSFontWeightSemibold)
        self.period_total = label(
            report, "", ((256, 54), (160, 26)), TOTAL_SIZE / report_scale, kit.NSFontWeightSemibold, digits=True
        )
        self.period_total.setAlignment_(kit.NSTextAlignmentRight)
        today = date.today()
        label(report, "С", ((16, 23), (15, 20)), secondary=True)
        self.first_date = date_picker(report, today.replace(day=1), "Начало периода", ((35, 20), (137, 24)), self)
        label(report, "по", ((188, 23), (19, 20)), secondary=True)
        self.last_date = date_picker(report, today, "Конец периода", ((211, 20), (137, 24)), self)
        self.period_error = label(report, "", ((16, 2), (400, 17)), 11)
        self.period_error.setTextColor_(kit.NSColor.systemRedColor())

        self.main_button = button(
            view, "", ((24, 16), (content_width, 76)), self, "toggleTimer:",
            font_size=30, fill=STOPPED_GRADIENT, text_color=kit.NSColor.whiteColor(), corner_radius=34,
        )

    @objc.python_method
    def _build_compact_panel(self):
        width, height = 272, 76
        padding = 2 * CompactButton.shadow_padding
        button_frame = ((0, 0), (width + padding, height + padding))
        self.panel = CompactPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            button_frame, kit.NSWindowStyleMaskBorderless, kit.NSBackingStoreBuffered, False
        )
        self.panel.setTitle_(APP_NAME)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(kit.NSColor.clearColor())
        self.panel.setHasShadow_(False)
        self.panel.setFloatingPanel_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setCollectionBehavior_(kit.NSWindowCollectionBehaviorMoveToActiveSpace)
        self.compact_button = button(
            self.panel.contentView(), "", button_frame, self, "toggleTimer:",
            font_size=30, fill=STOPPED_GRADIENT, text_color=kit.NSColor.whiteColor(), corner_radius=height / 2,
            button_class=CompactButton,
        )

    @objc.python_method
    def _build_menus(self):
        menu_bar = kit.NSMenu.alloc().init()
        application_menu = kit.NSMenu.alloc().initWithTitle_(APP_NAME)
        application_menu.addItemWithTitle_action_keyEquivalent_(f"Завершить «{APP_NAME}»", "terminate:", "q")
        menu_bar.addItem_(kit.NSMenuItem.alloc().init())
        menu_bar.itemAtIndex_(0).setSubmenu_(application_menu)

        view_menu = kit.NSMenu.alloc().initWithTitle_("Вид")
        self.view_item = view_menu.addItemWithTitle_action_keyEquivalent_("Компактный режим", "switchView:", "m")
        self.view_item.setTarget_(self)
        self.view_item.setKeyEquivalentModifierMask_(kit.NSEventModifierFlagCommand | kit.NSEventModifierFlagShift)
        view_item = kit.NSMenuItem.alloc().init()
        menu_bar.addItem_(view_item)
        view_item.setSubmenu_(view_menu)
        self.app.setMainMenu_(menu_bar)

        panel_menu = kit.NSMenu.alloc().init()
        expand = panel_menu.addItemWithTitle_action_keyEquivalent_("Развернуть окно", "switchView:", "")
        expand.setTarget_(self)
        panel_menu.addItemWithTitle_action_keyEquivalent_("Завершить работу", "terminate:", "")
        self.compact_button.setMenu_(panel_menu)

    @objc.python_method
    def _set_compact(self, compact):
        self.compact = compact
        if compact:
            if not self.panel_positioned:
                frame = self.window.frame()
                self.panel.setFrameTopLeftPoint_((frame.origin.x, frame.origin.y + frame.size.height))
                self.panel_positioned = True
            self.panel.makeKeyAndOrderFront_(self)
            self.window.orderOut_(self)
        else:
            self.window.deminiaturize_(self)
            self.window.makeKeyAndOrderFront_(self)
            self.panel.orderOut_(self)
        self.view_item.setTitle_("Развернуть окно" if compact else "Компактный режим")
        self.tick_(None)

    @objc.python_method
    def _refresh(self):
        running = self.tracker.is_running
        today = date.today()
        today_duration = self.tracker.total(today, today)
        hours = today_duration // (60 * 60 * MICROSECONDS_PER_SECOND)
        fill = RUNNING_GRADIENT if running else STOPPED_GRADIENT
        self.main_button.set_style("Остановить таймер" if running else "Начать отсчёт", fill)
        compact_action = "Отдыхать" if running else "Работать"
        self.compact_button.set_style(f"{hours}\u2002{compact_action}", fill)
        self.compact_button.setToolTip_(COMPACT_HINT)
        self.status.setStringValue_("Идёт учёт времени" if running else "Таймер остановлен")
        self.status.setTextColor_(kit.NSColor.systemGreenColor() if running else kit.NSColor.secondaryLabelColor())
        if self.compact:
            return

        monday = today - timedelta(days=today.weekday())
        self.today_total.setStringValue_(format_duration(today_duration))
        self.week_total.setStringValue_(format_duration(self.tracker.total(monday, monday + timedelta(days=6))))
        first = date.fromtimestamp(self.first_date.dateValue().timeIntervalSince1970())
        last = date.fromtimestamp(self.last_date.dateValue().timeIntervalSince1970())
        if first > last:
            self.period_total.setStringValue_("—")
            self.period_error.setStringValue_("Дата начала должна быть раньше даты конца или совпадать с ней.")
        else:
            self.period_total.setStringValue_(format_duration(self.tracker.total(first, last)))
            self.period_error.setStringValue_("")

    @objc.IBAction
    def switchView_(self, sender):
        self._set_compact(not self.compact)

    @objc.IBAction
    def periodChanged_(self, sender):
        self.tick_(None)

    @objc.IBAction
    def toggleTimer_(self, sender):
        try:
            if self.tracker.is_running:
                self.tracker.stop()
            else:
                self.tracker.start()
        except sqlite3.Error as error:
            show_alert("Не удалось изменить состояние таймера", str(error))
        self.tick_(None)

    @objc.IBAction
    def tick_(self, sender):
        try:
            self.tracker.checkpoint()
            self._refresh()
        except sqlite3.Error:
            self.status.setStringValue_("Ошибка доступа к базе данных")
            self.status.setTextColor_(kit.NSColor.systemRedColor())
            self.compact_button.setToolTip_(f"Ошибка доступа к базе данных. {COMPACT_HINT}")

    def applicationDidFinishLaunching_(self, notification):
        self._set_compact(False)
        self.app.activateIgnoringOtherApps_(True)
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1, self, "tick:", None, True)
        NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        self._set_compact(False)
        return True

    def windowShouldClose_(self, window):
        self.app.terminate_(self)
        return False

    @objc.typedSelector(b"Q@:@")
    def applicationShouldTerminate_(self, app):
        try:
            self.tracker.close()
        except sqlite3.Error as error:
            show_alert("Не удалось сохранить время перед выходом", str(error))
            return kit.NSTerminateCancel
        if self.timer is not None:
            self.timer.invalidate()
        return kit.NSTerminateNow


def main():
    app = kit.NSApplication.sharedApplication()
    app.setActivationPolicy_(kit.NSApplicationActivationPolicyRegular)
    try:
        tracker = TimeTracker(DB_PATH)
    except TrackerAlreadyOpen:
        show_alert("Приложение уже запущено", "Откройте работающее окно через Dock.")
        return 1
    except (OSError, sqlite3.Error) as error:
        show_alert("Не удалось открыть базу данных", str(error))
        return 1

    controller = AppController.alloc().init()
    controller.configure(app, tracker)
    app.setDelegate_(controller)
    app.run()
    return 0
