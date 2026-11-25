import sqlite3
from datetime import date, timedelta
from pathlib import Path

import AppKit as kit
import objc
from Foundation import NSDate, NSLocale, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

from time_tracking.tracker import (
    MICROSECONDS_PER_SECOND,
    TimeTracker,
    TrackerAlreadyOpen,
    format_duration,
    start_of_day,
)


APP_NAME = "Учёт времени"
DB_PATH = Path.home() / "Library" / "Application Support" / "Time Tracking" / "time_tracking.sqlite3"
COMPACT_HINT = "Правый клик или ⌘⇧M — развернуть окно."


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


def button(view, title, frame, target, action):
    control = kit.NSButton.buttonWithTitle_target_action_(title, target, action)
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
        self._build_main_window()
        self._build_compact_panel()
        self._build_menus()

    @objc.python_method
    def _build_main_window(self):
        style = kit.NSWindowStyleMaskTitled | kit.NSWindowStyleMaskClosable | kit.NSWindowStyleMaskMiniaturizable
        self.window = kit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (480, 350)), style, kit.NSBackingStoreBuffered, False
        )
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitle_(APP_NAME)
        self.window.setDelegate_(self)
        self.window.center()
        view = self.window.contentView()

        label(view, "Рабочее время", ((24, 298), (275, 33)), 27, kit.NSFontWeightSemibold)
        self.status = label(view, "", ((25, 271), (330, 20)), secondary=True)
        compact = button(view, "Компактно", ((346, 299), (112, 30)), self, "switchView:")
        compact.setToolTip_("Свернуть окно до кнопки таймера")

        for index, title in enumerate(("Сегодня", "На этой неделе")):
            summary = card(view, ((24 + index * 222, 174), (210, 79)))
            label(summary, title, ((16, 51), (179, 19)), secondary=True)
            value = label(summary, "", ((16, 12), (179, 33)), 25, kit.NSFontWeightSemibold, digits=True)
            if index == 0:
                self.today_total = value
            else:
                self.week_total = value

        report = card(view, ((24, 66), (432, 96)))
        label(report, "За выбранный период", ((16, 59), (239, 23)), 15, kit.NSFontWeightSemibold)
        self.period_total = label(report, "", ((256, 55), (160, 31)), 22, kit.NSFontWeightSemibold, digits=True)
        self.period_total.setAlignment_(kit.NSTextAlignmentRight)
        today = date.today()
        label(report, "С", ((16, 23), (15, 20)), secondary=True)
        self.first_date = date_picker(report, today.replace(day=1), "Начало периода", ((35, 20), (137, 24)), self)
        label(report, "по", ((188, 23), (19, 20)), secondary=True)
        self.last_date = date_picker(report, today, "Конец периода", ((211, 20), (137, 24)), self)
        self.period_error = label(report, "", ((16, 2), (400, 17)), 11)
        self.period_error.setTextColor_(kit.NSColor.systemRedColor())

        self.main_button = button(view, "", ((24, 16), (432, 38)), self, "toggleTimer:")
        self.main_button.setControlSize_(kit.NSControlSizeLarge)

    @objc.python_method
    def _build_compact_panel(self):
        self.panel = CompactPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (144, 44)), kit.NSWindowStyleMaskBorderless, kit.NSBackingStoreBuffered, False
        )
        self.panel.setTitle_(APP_NAME)
        self.panel.setFloatingPanel_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setCollectionBehavior_(kit.NSWindowCollectionBehaviorMoveToActiveSpace)
        self.compact_button = button(self.panel.contentView(), "", ((4, 3), (136, 38)), self, "toggleTimer:")
        self.compact_button.setControlSize_(kit.NSControlSizeLarge)

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
            top_left = self.window.frame()
            self.panel.setFrameTopLeftPoint_((top_left.origin.x, top_left.origin.y + top_left.size.height))
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
        self.main_button.setTitle_("Остановить таймер" if running else "Начать отсчёт")
        self.compact_button.setTitle_("Остановить" if running else "Начать")
        self.compact_button.setToolTip_(COMPACT_HINT)
        self.status.setStringValue_("Идёт учёт времени" if running else "Таймер остановлен")
        self.status.setTextColor_(kit.NSColor.systemGreenColor() if running else kit.NSColor.secondaryLabelColor())
        if self.compact:
            return

        today = date.today()
        monday = today - timedelta(days=today.weekday())
        self.today_total.setStringValue_(format_duration(self.tracker.total(today, today)))
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
