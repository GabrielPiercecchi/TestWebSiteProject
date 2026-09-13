document.addEventListener('DOMContentLoaded', () => {
  const header = document.querySelector('.site-header');
  const revealItems = document.querySelectorAll('.reveal');

  const updateHeader = () => {
    header?.classList.toggle('is-scrolled', window.scrollY > 16);
  };

  updateHeader();
  window.addEventListener('scroll', updateHeader, { passive: true });

  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver((entries, currentObserver) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          currentObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12 });
    revealItems.forEach((item) => observer.observe(item));
  } else {
    revealItems.forEach((item) => item.classList.add('is-visible'));
  }

  document.querySelectorAll('.button, .nav-cta').forEach((button) => {
    button.addEventListener('click', () => {
      button.classList.add('is-pressed');
      window.setTimeout(() => button.classList.remove('is-pressed'), 180);
    });
  });

  document.querySelectorAll('.password-field').forEach((field) => {
    let hideTimer;
    field.addEventListener('input', () => {
      window.clearTimeout(hideTimer);
      field.type = 'text';
      hideTimer = window.setTimeout(() => {
        field.type = 'password';
      }, 850);
    });
  });

  const bookingPage = document.querySelector('#booking-form');
  if (bookingPage) {
    const checkIn = document.querySelector('#check-in');
    const checkOut = document.querySelector('#check-out');
    const guestCount = document.querySelector('#guest-count');
    const roomOptions = document.querySelector('#room-options');
    const roomCount = document.querySelector('#room-count');
    const noRooms = document.querySelector('#no-rooms');
    const calendar = document.querySelector('#selected-calendar');
    const calendarTitle = document.querySelector('#calendar-title');
    const sourceRooms = [...document.querySelectorAll('.room-option')].map((room) => ({
      element: room,
      id: room.dataset.roomId,
      capacity: Number(room.dataset.capacity),
      name: room.querySelector('strong').textContent,
    }));

    const renderCalendar = (bookings, name) => {
      calendar.replaceChildren();
      calendar.classList.remove('empty-calendar');
      calendarTitle.textContent = name;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const monthFormatter = new Intl.DateTimeFormat('it-IT', { month: 'long', year: 'numeric' });
    const dayFormatter = new Intl.DateTimeFormat('it-IT', { day: '2-digit', month: '2-digit' });
      const heading = document.createElement('div');
    heading.className = 'calendar-heading';
    heading.textContent = `Disponibilità da ${monthFormatter.format(today)}`;
      calendar.appendChild(heading);

      const grid = document.createElement('div');
    grid.className = 'calendar-days';
      for (let offset = 0; offset < 42; offset += 1) {
      const current = new Date(today);
      current.setDate(today.getDate() + offset);
      const iso = current.toISOString().slice(0, 10);
      const isBusy = bookings.some((booking) => iso >= booking.check_in && iso < booking.check_out);
      const day = document.createElement('span');
      day.className = `calendar-day ${isBusy ? 'is-busy' : 'is-available'}`;
      day.title = `${dayFormatter.format(current)} · ${isBusy ? 'Occupata' : 'Disponibile'}`;
      day.textContent = current.getDate();
        grid.appendChild(day);
      }
      calendar.appendChild(grid);
    };

    const refreshRooms = async () => {
      const params = new URLSearchParams({ check_in: checkIn.value, check_out: checkOut.value, guests: guestCount.value });
      const response = await fetch(`/api/availability?${params}`);
      if (!response.ok) return;
      const payload = await response.json();
      const availableIds = new Set(payload.rooms.map((room) => String(room.id)));
      sourceRooms.forEach(({ element, id }) => {
        element.hidden = !availableIds.has(id);
        if (element.hidden && element.querySelector('input').checked) {
          element.querySelector('input').checked = false;
          calendar.replaceChildren();
          calendar.classList.add('empty-calendar');
          calendarTitle.textContent = 'Seleziona una camera';
        }
      });
      roomCount.textContent = payload.rooms.length;
      noRooms.classList.toggle('hidden', payload.rooms.length !== 0);
    };

    [checkIn, checkOut, guestCount].forEach((field) => field.addEventListener('change', refreshRooms));
    roomOptions.addEventListener('change', async (event) => {
      const selected = event.target.closest('.room-option');
      if (!selected) return;
      const params = new URLSearchParams({ room_id: selected.dataset.roomId });
      const response = await fetch(`/api/availability?${params}`);
      const payload = await response.json();
      renderCalendar(payload.bookings, selected.querySelector('strong').textContent);
    });
  }
});
